# B05D ordinary private chat research

## Design recorded before implementation

Starting revision: `6aefd82610c5a7fb7f02e677bf87d15e72eaf7e2` (PR77/78).
Read INT22, INT23, B05A, B05C and B09D and traced private_web CLI →
WorkerConversationRuntime → PublicResearchBridge → authenticated AF_UNIX →
public_research_worker → hosted search → B05C assessment → screened durable
Russian reply. B05A already authorizes repeated topic selection for one bound
participant/source within finite topic/aggregate limits; no per-message approval
is needed. It does not authorize other participants or free-form private research.

Actual gaps: no shipped ordinary deployment wiring; incompatible research and
pilot Stop formats; risk of replacing protected Compose commands; unverified
connected accounting/control behavior under deployment identities. Reuse the
pinned app/worker Dockerfiles and all existing IPC, catalog, journal and stores.
Add a dedicated research override and a preparation helper that derives the app
command from the resolved existing stack, appending only existing research CLI
options. Never replace provider-worker's command or its historical allowance
selection. Preparation reads configuration only, creates no databases or authority,
and refuses to overwrite its output. Use an explicit pilot-Stop option in the
research worker to select the existing PilotStop parser; retain INT23's separate
public Stop parser for existing deployments. Thus B09D's one-way worker assertion
and telemetry cover the exact Stop used by both workers. Missing control IPC
continues to report unavailable/uncertain worker confirmation and application
Stop independently; no new clear or renew action exists.

Use the existing worker provider.sqlite3 for both provider processes. Inference
attempt keys are `operation:attempt`; public research keys are `operation`.
Application intent/started evidence remains in ipc-state.db and is never added
to worker token totals. Existing worker telemetry reads each committed provider
row exactly once, reports missing usage and unresolved reservations, and preserves
historical rows. The worker-only journal separately consumes catalog slots before
contact. Both worker processes share spend suspension. No startup can replace a
missing provider database or journal, reset allowance or clear Stop. Existing
profile expiry/revocation and uncertain-response rules remain enforced.

Research provider timeout is 30s, overall 70s, IPC client 80s; the inference
worker is 30s/40s with a 320s IPC ceiling. Calls are sequential. Protected proxy
and client timeouts must accommodate this path, and queueing at the single WSGI
listener; their real values are unavailable here. No routing change is proposed.
B05C metadata is retained whole in the participant report, with uncertainty,
contradictions, same-operation model provenance and unavailable page content.
Oversized reports fail closed without truncating disagreements; the existing
private reply limit remains a known bound.

Known host facts supplied by the operator: AI-Server ordinary pilot lives at
`/srv/almost-reasonable/int22`; app 11022:11022, worker 11021:11021, IPC group
11020. Existing inputs are base pilot Compose followed by protected
`int22.proxy.yaml`, `int22.network.yaml`, `int22.multiturn-20261022-02.yaml`;
the last selects historical allowance state. App internal address is
`172.24.0.3:8000`. Cloudflare, SERVER/nginx and SSH routing stay unchanged.
Protected files, deployed image pins, file inventory and effective proxy timeouts
are not in this checkout. All paths beyond these facts require host verification.

Verification will reuse existing synthetic provider, authenticated chat and owner
fixtures: shipped Compose resolution and real containers with numeric IDs,
canonical-only request inspection, assessed reply/history, shared usage, common
Stop, restart and uncertain-response no-retry behavior. No production credential,
live provider, deployment or paid check is authorized in this task.

## Connected renderer design

Connected verification exposed a second gap: long opaque provider citation IDs
can exhaust the private encoded-leakage screening budget. Validate original
source/evidence bindings, project references to short reply-local labels, then
screen all visible report fields. Preserve every URL, claim, relationship,
contradiction, uncertainty and assessment-origin field without truncation.
`reference_scope=reply_local` distinguishes these labels from provider IDs.
Provider accounting retains the response ID. No privacy-screen budget, output
limit, authority or Russian gate changes. Test with long IDs and private context.

Research-enabled app startup and research-worker startup also require current
initialized schemas through read-only admission before any store constructor.
An empty/corrupt existing file is rejected without initializing a replacement.
Configuration preparation still reads no databases. Synthetic fixtures explicitly
initialize their own stores; deployed startup does not.

## Later operator runbook

No deployment, credential access or paid call was performed by this task. Use
the ordinary deployment/domain and retained B09D owner project.

1. Record the approved commit, image pins, consistent state backup and protected
   resolved configuration. Define `pilot` as the **existing command array** with
   its project name, protected env and all four Compose inputs in their original
   order. Retain `int22.multiturn-20261022-02.yaml` and its historical allowance.
   Define `controls` as the retained B09D command, including protected overrides.
   Assert both Stops through the existing authenticated owner emergency action;
   require separate application/worker confirmations. Keep inference stopped.
   Preserve application/provider stores, WAL/SHM, activation, allowance, journal
   and owner audit/session state. Never initialize substitute production stores.

2. Copy `ops/private-research.env.example` to protected host configuration
   (`0600`). Use the original env file first and research env second; remove
   ambient `PILOT_*`/`COMPOSE_*` overrides. Fill approved release tags/digests,
   reviewed project/organization and verified absolute paths. Stage images built
   with the existing pinned Dockerfiles without starting them. Require the
   retained inference, app and owner images to support the reviewed EventStore
   schema, including B05C; this PR introduces no schema migration. Unprovided
   older deployed binaries may require a separately reviewed baseline upgrade.
   Keep a compatible ordinary app image for rollback. Verify inference and B09D
   telemetry both select `/var/lib/pilot-worker/provider.sqlite3`.

3. Provision only new, non-colliding directories: research IPC `11021:11020 0710`,
   application catalog `11022:11022 0700`, worker catalog `11021:11021 0700`.
   Ancestors must meet existing trusted-path rules. Do not recursively change
   existing state ownership. Place each `profile.json` projection as its service
   owner, `0600`, using the exact [B05A contract](b05a-reusable-public-research.md):
   peer UID **11022**, matching deployment scope, current bounded interval,
   reviewer, finite canonical questions and operation limits. Application-only
   fields bind the exact existing participant/source; compare all common fields
   and exclude private bindings from the worker projection. Public-data review
   is substantive; parser/model output cannot grant it. The current CLI supports
   one participant/source profile, not population-wide authority. A valid profile
   needs no per-message operator approval.

   Preserve an existing `research-authority.sqlite3` in worker state. Only for a
   genuinely new, explicitly approved catalog installation, exclusively create
   that new empty file once as `11021:11021 0600`; startup installs the existing
   schema and records the reviewed profile. A missing previously used journal
   requires recovery, not replacement. Preparation/startup never runs INT22
   `prepare`, initializes provider DBs, resets counters, renews inference
   activation, restores snapshots or clears Stop. Expired inference remains a
   blocker even with a valid research catalog. Credentials stay in the existing
   worker-only secret mount.

4. Derive the application command from the resolved original stack. `pilot`
   includes both protected env files but still only the original four Compose
   files. Set a new absolute protected output path and the exact profile scope:

   ```bash
   : "${RESEARCH_APP_COMMAND:?new absolute protected output path}"
   : "${RESEARCH_SCOPE:?scope matching both reviewed projections}"
   "${pilot[@]}" config --format json |
     python -B -m app.ops.private_research_config \
       --scope "$RESEARCH_SCOPE" --output "$RESEARCH_APP_COMMAND"
   research=("${pilot[@]}" -f ops/compose.private-research.yaml \
     -f "$RESEARCH_APP_COMMAND")
   "${research[@]}" config --quiet
   ```

   The helper exclusively creates a `0600` file and refuses overwrite,
   pre-enabled research and incompatible commands/identities. Compatible memory
   and narrative flags, including their existing permission path and pilot ID,
   are preserved when research options are appended.
   It reads configuration only: no stores, credentials, authority or requests.
   Regenerate a new reviewed file after any original command change. Inspect
   the resolved diff: provider-worker must remain entirely unchanged, especially
   image/command/allowance. Private-app retains all original flags, proxy settings,
   networks, ports, mounts and `172.24.0.3:8000`; additions are its reviewed image,
   research mounts/options and research service. Cloudflare, SERVER/nginx, SSH
   and DNS remain unchanged.

5. Inspect effective HTTP/proxy timeouts before startup. Inference uses
   30s/40s attempt/overall timeouts with a 320s IPC ceiling; research uses 30s/70s
   and an 80s IPC deadline. Allow for sequential calls, IPC/storage, existing
   memory stages and serial WSGI queueing. Browser fetch has no abort timer.
   Actual host proxy/client values are unprovided; insufficient timeouts require
   separate operator resolution, not an implicit routing change. A dropped HTTP
   connection never authorizes a fresh retry of an uncertain operation.

6. Under later explicit deployment authorization, leave Stops asserted and the
   retained owner/inference services untouched. Start research, verify its
   listening socket `11021:11020 0660`, then recreate only the app:

   ```bash
   "${research[@]}" up -d --no-deps --no-build --pull never research-worker
   # Verify socket/process readiness before the next command.
   "${research[@]}" up -d --no-deps --no-build --pull never private-app
   "${research[@]}" ps -a research-worker private-app
   ```

   Verify app isolation, shared provider DB and pilot Stop, B09D usage/status and
   both emergency acknowledgements. Missing/expired/malformed authority, revoked
   IDs, absent stores and unsafe files fail closed. Startup makes no paid call.
   Research explicitly selects the **pilot** parser using `--pilot-stop-file`;
   INT23's `--stop-file` retains its different public-research contract. Never
   interchange them. Worker confirmation covers the common gate for both paths,
   not cancellation of a provider call already in flight. Both workers recheck
   Stop before releasing output. Lost/missing control acknowledgement remains
   uncertain/unavailable even when application Stop succeeds; absent telemetry
   is not zero usage or readiness. Emergency control cannot clear worker Stop,
   renew authority or replenish slots.

## Restart and disabling

Use the same complete research command and current stores. With B09E, the
ordinary app, owner console, inference and research workers may start or restart
together against their existing shared SQLite stores on the supported Linux
host. Check both worker sockets, application HTTP and owner telemetry before
sending requests. Readiness checks remain necessary; process ordering is no
longer a storage correctness requirement. The separate B09D project can stay
running during an ordinary extension restart. No restart clears Stop or
consumes/renews authority merely by starting.

B09E's offline Docker fixture starts all six services together and restarts them
together, with fake providers, production numeric identities and native volume
state. It preserves exact provider usage, historical reservations, allowance,
research admission and Stop, and checks integrity/foreign keys in all seven
stores. This fixture passed without retrying failed startups. It does not prove
every filesystem or deployment safe; the actual AI-Server configuration and
live/browser validation remain operator work.

Earlier app/owner initialization and inference/research restarts reported
SQLite SIGBUS, including on native storage. B09E independently proves and fixes
transaction lock loss caused by application security handles. SIGBUS did not
reproduce in the corrected simultaneous-start/restart fixture. That evidence
does **not** establish the exact cause of the earlier SIGBUS failures. See the
[SQLite lock/security evidence](eventstore-v4.md#sqlite-security-handles-and-concurrent-processes-b09e).

For an abrupt exit, stop the specific service and verify no process/container
owns its socket before unlinking **only that stale socket** as the worker UID.
Preserve databases and journal. Consumed/uncertain operations and observed
revocation survive restart; a new profile ID requires an independent decision.

To disable, assert owner emergency Stop and verify both outcomes; stop only
`research-worker` using the complete research stack. Recreate only `private-app`
with the retained original four-file `pilot` command and compatible ordinary
image, omitting both research overrides: `up -d --no-deps --no-build --pull never
private-app`. Do not use `down` or `--remove-orphans`. Keep projections, journal,
provider rows and all app state protected in place. Earlier research replies can
be held as `research_denied` without their original bridge/authority; their data
is preserved. Rollback never restores old databases, clears Stops or starts
inference automatically.

## Later single budgeted live check — not performed

Record separate approval for the exact release, normal domain, synthetic
account/source, canonical question, billing scope, validity window, one inference
and at most one search, explicit currency budget and shutdown owner. If retained
memory stages can add calls, budget those explicitly or stop this procedure;
do not silently disable unrelated features. Verify current provider access,
charges and remaining budget. Require existing valid inference activation and
allowance plus the reviewed catalog; exhaustion/expiry needs a separate authority
decision, never automatic renewal.

Record baseline usage/journal/allowance under their service identities. Only then
may the operator release independent Stops under the existing procedure and send
one synthetic message through normal authenticated HTTPS chat. Check Russian
reply/history, source URLs, contradictions, uncertainty, `same_operation_model`,
`source_content=unavailable`, `unverified`, `semantic_support=not_evaluated` and
reply-local citation relationships. Another model action or unavailable result
is a failed check, not permission for another paid message.

Compare worker provider rows/tokens (one inference plus at most one search),
separately from app IPC intent records. Missing cached tokens and tool charges
are not invented; telemetry has no dollar-budget enforcement. Lost/uncertain
responses consume authority: inspect instead of retrying with a new message ID.
Assert owner emergency Stop, confirm both outcomes and shared worker Stop, stop
research and retain sanitized evidence. This PR authorizes no live check,
credential access, production deployment or Meta/OpenAI call.

## Offline evidence and limits

`test_b05d_bootstrap.py` covers rejection of empty/corrupt substitute stores.
`test_b05d_research_config.py` covers exclusive offline preparation and protected
flag preservation. `test_b05d_research_deployment.py` resolves shipped Compose
with a synthetic historical allowance override, verifies binds/network/ports,
then uses test-only no-egress, fake-provider and daemon-native volume overrides
with real 11022/11021/11020 identities and sockets. It reuses B09D lifecycle,
Browser/privacy acknowledgement and B05C assessed fixtures with long provider
IDs. It verifies persisted assessment/history, historical plus new usage once,
restart preservation, lost-response no-retry, exhaustion, shared Stop, unavailable
controls, permanent revocation and disabling without state loss. Container-side
execution is separate from Python coverage; existing INT22/INT23/B05A/B05C/B09D
unit/connected tests cover those branches. Docker skips explicitly report an
absent CLI/Compose/daemon and establish no container evidence.

Private reports retain the 4096-byte bound and finite privacy-screen budget;
large reports/complex URLs can fail closed without truncating disagreements.
Original reports are not mutated; reply-local references preserve every source
binding and assessment field. Same-operation model assessments provide no
independently fetched page content or entailment guarantee. Real model quality,
protected overrides, image/schema compatibility, host permissions/egress,
proxy timeouts and full-host restart behavior require later operator validation.

Original B05D validation: `scripts/ci.sh` passed **5,395 tests** with **100% line and
branch coverage**, lint and formatting, using eight pytest workers and an
isolated environment with the pinned development dependencies. No Docker checks
were skipped. Both existing pinned Dockerfiles built; the worker CLI also loaded
with networking disabled. No production credential, paid call or deployment was
used. That original evidence covered sequenced readiness. B09E extends this
fixture to simultaneous startup/restart and integrity checks as described above;
the unprovided AI-Server configuration remains outside the offline evidence.


## B05E: memory, narrative and research in one conversation

Starting revision: `0dceeaf91728ffb6fe667d44729c83726c32d978`. The design
trace found explicit incompatibility in `WorkerConversationRuntime` and
`app.ops.private_research_config`. The private_web CLI already forwards all
three capabilities and omits shared intelligence when narrative is enabled.
Narrative-context IPC and provider request validation already accept the ordinary
research proposal vocabulary, retain fiction labels and direct-reality question
instructions, and disable inference tools. `PublicResearchBridge` resolves only
an existing reviewed topic or exact question, bound to one participant/source;
no private context is needed by its worker protocol.

Removing the two prohibitions alone would commit an unacknowledged fictional
episode whenever a research report persisted. The retained order is memory
(evolution or extraction), typed memory, an explicitly cued narrative proposal,
then inference. A research decision uses the existing durable research intent
as the alternative-reply marker. After intent, a received narrative proposal is
rejected and its private receipt erased; recovery applies the same rule even
if interruption preceded rejection. Only an ordinary persisted reply can
acknowledge its projected episode. Existing episode/source dependencies remain
on research replies, so deleting their private sources erases those replies.

`PilotRequestGate` and `PilotAllowance` retain the atomic narrative/reply pair.
Inference claims the held reply slot once even when it selects research; the
research worker consumes its separate catalog slot. Unused held reply slots and
uncertain operations remain consumed. Intent, started and provider reservation
identities prevent retries after interruption, restart or a lost response.
Application IPC markers remain separate from the shared worker usage ledger.
No worker permission, activation, operation limit, schema or authority changes.
Shared proposals remain incompatible with narrative mode.

Public search still receives only the sealed canonical question, never message
text, factual memory, episode text or participant identifiers. The application
screens report output against source messages and the actual inference context,
including selected memory prose, historical replies and projected fiction.
This comparison data stays local. B05C sources, contradictions, uncertainty and
same-operation assessment limitations are retained whole. Existing reply-size
and privacy-screen bounds can still reject a report; no truncation or weaker
screening is introduced.

The existing four-input production preparation procedure above applies unchanged:
retain base Compose, `int22.proxy.yaml`, `int22.network.yaml`, and
`int22.multiturn-20261022-02.yaml`, then append the two research overrides.
The helper preserves compatible memory/narrative flags and leaves the inference
command, historical allowance, existing stores, common worker Stop and B09D
accounting intact. Existing valid inference/narrative permissions and the finite
reviewed catalog are required together. This task neither renews them nor adds
a mode flag, separate deployment, public scheduling or Threads operation.

Offline regressions in `tests/test_b05e_combined.py` connect authenticated HTTP,
real AF_UNIX inference/research workers, existing pilot permissions/allowance,
SQLite state and fake providers. One configured conversation corrects a factual
memory, creates fiction, selects approved research with a same-turn fictional
proposal, restarts, recalls the corrected fact, distinguishes fiction from real
activity, and continues the story. Checks inspect actual provider requests,
replies, durable episodes/dependencies, deletion, operation totals, participant
isolation, Stop, permission loss and interruption/lost-response replay. CLI and
preparation regressions cover retaining the same configuration.

No live combined journey, deployment, production credential access, authority
renewal or OpenAI/Meta call was performed. A later separately authorized check
must use the ordinary deployment/domain and explicitly budget every enabled
memory/narrative/inference operation plus research. Real-model story continuity,
truthful reality answers, research quality, physical iPhone behavior, protected
host configuration, permissions, proxy timeouts and restart readiness remain
live/operator validation work. Fake-provider replies demonstrate data flow and
contracts, not real-model semantic reliability; complete MVP remains BLOCKED.
