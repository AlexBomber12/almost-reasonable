# Standing public operation with a monthly budget

For the reviewed single-project $40 policy covering private Sol, public Sol and
Luna together, use [the shared budget cutover](shared-project-budget.md). The
legacy lane-specific limits below apply only before that explicit migration.

This mode requires explicit operator authorization. Existing finite windows and
v1/v2 research profiles retain their original expiration and lifetime allowances.
It does not activate itself from environment variables or model output.

The existing OpenAI project and API credential can serve both public and private
workers. The local public budget excludes private dialogue calls. An OpenAI
project hard limit counts **all** project traffic, including private dialogues;
it is an additional provider safeguard, not the public wallet. Do not claim that
the provider project isolates costs when a shared project is used. OpenAI spend
enforcement can lag; see the [official spend-limit guide](https://developers.openai.com/api/docs/guides/spend-limits).

## Limits

- UTC calendar month: Sol $15, Luna research $5; the allocations total $20.
- At most 100 public search requests per UTC month, one hosted web search per request.
- Each daily lease retains at most 96 model calls, 24 poll pages, 24 inbound
  events, six publications and 36 publication HTTP requests.
- Research questions, evidence, mission evolution, publication guards and owner
  controls retain their existing native validation. A quiet/no-action decision
  remains valid. This change does not require the agent to publish.

The worker reserves a conservative maximum **before** calling the SDK. Known
usage settles at conservative prices in integer microdollars. Sol requests are
bounded to 128 KiB of serialized SDK arguments plus tokenization overhead;
research reserves the entire Luna context, including search content. Unsupported
models, tools, tiers, oversized requests and malformed usage do not release funds.
The rate ceilings include cache writes and regional uplift, with the long-context
Luna multiplier; [pricing](https://developers.openai.com/api/docs/pricing) and the
[Luna model limits](https://developers.openai.com/api/docs/models/gpt-6-luna) were
checked on 2026-11-02. These are conservative admission estimates, not invoice data
or a guarantee against future provider pricing changes. Review rate ceilings when
models or provider prices change. The shared project hard limit remains advisable.

A lost/unknown response keeps its whole reservation, including in subsequent
months. This can pause earlier than billed usage alone would imply. A response
settled across a month boundary is charged to the settlement month; original
admission month and replay identity remain recorded. Known settled spending and
search counts get a new allowance in the next UTC month. Deleting, recreating,
changing or replacing a wallet is **not** an authorized way to replenish it.
Missing or unsafe state fails closed. Existing provider suspensions and Stops
are not automatically cleared by calendar rollover.

## Provisioning and activation

1. Keep the existing credential files and project scope. Do not put credentials
   in source files, command arguments, chat, examples or CI. Preserve the private
   services and their independent authority.
2. Back up the public databases with SQLite backup, integrity-check the copies,
   and hold the public native Stops before changing worker or runtime images.
3. As the model worker UID, initialize an absent, worker-owned wallet once:
   `MonthlyBudget.initialize(absolute_path, lane="sol", project_id=reviewed_project)`.
   As the research worker UID, use `lane="research"` and a separate file. Parent
   directories must be owned by that UID and mode 0700; wallet files are 0600.
   Runtime constructors only open existing state and verify the pinned policy.
4. Set the Sol worker's mandatory protected `monthly_budget_path`. Pass the
   research worker `--monthly-budget /absolute/worker-owned/budget.sqlite`.
   A standing research worker refuses to start without that option. Each wallet
   binds its SDK project ID. Keep `max_retries=0` and existing native Stops.
5. Install matching application/worker public research projections with
   `version="public-research-profile-v3"`, `renewal="standing-monthly-utc"`, the
   autonomous public purpose, `aggregate_limit=100`, and
   `expires_at="9999-12-31T23:59:59.999999+00:00"`. Other protected profile fields,
   screened 512-byte question policy and role separation remain mandatory.
   The sentinel expiry is accepted only for this explicitly standing public
   version. First-month counters include historical public research admissions;
   replacement profiles cannot replenish the current month.
6. In the protected runtime configuration set `standing_public_reviewed=true`
   and add `standing_public` with `policy_id`, `account_ref`, `project_id`,
   `version="standing-public-v1"` and the exact budget version
   `public-usd-conservative-20261102-v1`. Preserve existing live/publication
   review flags and mission/research bindings. Use the same project ID in both
   wallets, both SDK configurations and this policy.
7. Run `app.ops.int24_threads_runtime run-continuous --activate-live --config ...`
   under the trusted application UID, with a service restart policy. The process
   authenticates the local operator, pins controls durably and issues/resumes only
   the current UTC day's finite grant/window. It never removes human Stops or
   revocations. A second runner exits busy, and restart resumes the same daily
   capacity. Preserve all existing unknown outcomes and consumed capacities.
8. Verify actual mounted configurations, UIDs, image/source identity, wallet
   policy and all native Stop endpoints before releasing maintenance-only Stops.
   Observe an ordinary cycle; do not spend funds on duplicate operator probes.

The standing runner permits append-only history beyond the original finite
journal lifetime capacity. Per-day admissions and monthly worker wallets remain
bounded; historical identities, revocations, unknown operations and dependent
evidence are retained. Monitor disk space and back up databases. Storage failures,
changed privacy/root controls and unexpected provider failures fail closed rather
than silently creating a fresh authority or state directory.
Event-claim lookup streams retained payloads instead of decoding the whole journal
at once. Mission recovery selects only mission decisions in the active window
before loading payloads; regression cases retain 5,000 large historical records.

## Stop and revocation

Owner Emergency Stop remains the primary way to stop the application and both
public workers. Requests already sent to the provider cannot be recalled.
`revoke-standing --activate-live --config ...` durably revokes standing renewal;
the active runner checks revocation before further effects. Revoking a generated
daily grant also prevents future standing renewal. None of these actions deletes
history, refunds an unknown call or creates permission to retry a lost publication.

## Interrupted Threads requests

A failed live container-creation request with auto-publication explicitly disabled
ends that operation without a publish request or retry. The next ordinary cycle
can continue under the same finite grant and budget. Its reserved publication and
HTTP counts are retained. This does not retry the failed draft or recover legacy
uncertainty automatically; see the [publication contract](b11b-threads-publication.md).

An actually uncertain publication or interrupted `SENDING` operation returns
`publication_paused` from its finite window. The standing runner stays alive and
checks local controls once per second while waiting for original-operation
reconciliation. It makes no model, search, polling or publication requests and
issues no new leases during that pause, including across midnight. Once the
outcome is resolved, it re-enters the current bounded lease, preserving consumed
allowances and history. Owner Stop, standing revocation, changed controls and
termination still stop the runner. A failed ordinary window retains its existing
fail-closed behavior; it is not silently retried.

The automated tests are offline regression evidence. They do not constitute real
model acceptance, participant testing, a confirmed publication or production
validation. Deployment and an observed normal cycle are separate evidence.

## Investigating rejected research

The source budget counts distinct recorded source URLs, not citation occurrences.
Responses may cite a page several times. Each occurrence is validated before
identical source metadata is folded into its first sealed source record. A later
malformed citation is retained as rejected evidence; conflicting metadata fails
closed. There is an independent hard limit of 32 citation annotations per response.
The public report still contains at most three sources and makes no independent
semantic-verification claim.

Worker diagnostics distinguish source-budget overflow, annotation overflow,
conflicting duplicate metadata, malformed assessment JSON, invalid assessment
fields and unknown/ambiguous assessment sources. These fixed codes reach the
owner console. Unrecognized parser errors retain the generic stage code. Rejected
provider prose, exception text and source URLs are not included in diagnostics.
Privacy, source, language and URL-binding checks continue to reject unsafe reports.
Historical generic rejection codes cannot identify these narrower causes after
the fact: judge improvement using ordinary cycles after deployment, including
both acceptance rate and whether accepted reports use their sources correctly.
