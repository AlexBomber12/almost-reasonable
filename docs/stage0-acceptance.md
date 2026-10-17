# INT-10 Stage 0 acceptance and release evidence

Stage 0 readiness is **BLOCKED**. The acceptance package executes all 12 scenario
mappings and distinguishes connected synthetic checks from remaining requirements.
The connected synthetic SCN-12 cross-channel matrix is complete; fluent-human
calibration and operator threat/risk review still prevent Stage 0 acceptance.
Complete MVP acceptance is also BLOCKED. Synthetic success does not grant
rollout authority.

## Reproduce

From the repository root with `requirements-dev.txt` installed:

```sh
python -m acceptance.run
```

The runner invokes `scripts/ci.sh`: Ruff plus full pytest with 100% application line
and branch coverage, including OAuth. The pytest plugin validates all 12 manifest
entries against collected exact node IDs and records actual setup/call/teardown
outcomes. Focused pytest commands remain available without the CI plugin.
Artifacts under ignored `.artifacts/stage0/` are `gate.log`, `tests.json`,
`coverage.json`, `evidence.json` and `report.md`. GitHub CI uploads the same package
for the checked-out current PR HEAD, without relying on historical green checks.
The package is local execution evidence; remote CI/review evidence belongs to the PR.

Exit 0 would mean PASS; exit 1 means the executed gate/evidence failed; exit 2 means
its deterministic gate passed while release readiness remains BLOCKED. CI permits
only 0/2 after this gate; no assertion, coverage threshold or failing check is waived.
A skipped/incomplete mapped test is NOT_RUN, never PASS. Old output files are removed
before execution; code/source changes during a run invalidate its result. Metadata
includes the evaluated HEAD, base, source digest and modified-tree status. A report
from an uncommitted tree is not evidence for a later commit without fresh execution.

The static [manifest](../acceptance/scenarios.json) is the only scenario status and
mapping inventory. Its PASS entries mean connected deterministic evidence in their
explicit synthetic scope; every complete-MVP entry remains BLOCKED. The normative
[behavior scenarios](behavior-scenarios.md) and [acceptance contract](mvp-acceptance.md)
still define the full requirements. Missing implementation is BLOCKED, not a rollout
deferral. Live OpenAI, enrollment and real Meta remain assigned to Stages 1–3.

## Evidence boundaries

The added WSGI journeys independently authenticate A/B, release Russian recorded
replies, extract private memory, promote approved closed shared records, reopen
stores and use later context. Source deletion invalidates private/shared revisions
and dependent cross-participant replies. Older SQLite/WAL backups replay the current
ledger into read-only quarantine; B's unrelated state remains available. A challenged
belief lowers confidence using counterevidence, stays unverified, persists into later
context and disappears after source deletion. Mission confirmation uses genuinely
later independent evidence and the actual mission adapter. INT-18 additionally
tests the explicitly configured Stage 0 coordinator consuming that mission for
scheduler and inbound Threads deliberation, persisting its binding, and holding
stale or invalid mission-dependent work after restart. This remains an offline
recorded-model/simulator composition; it is not a deployed daemon, live provider
path, real-model judgment, or completed human calibration.

Preserved mapped suites exercise actual dispatch failure classification, unknown and
unknown_abandoned, immutable identities, outbox/inbound acknowledgement, approval
revocation, stop/spend timing, source deletion, held model uncertainty, concurrency,
downtime coalescing and process exits. Process-exit evidence covers private reservation/result/release, deletion intent/
application, scheduler start/result/completion, mission-bound journal start/intent
and provider dispatch; other checkpoint exceptions establish
transaction/reopen behavior, not comprehensive OS-process crash coverage. Auth,
event/provider, privacy, owner, scheduler, Threads and journal commits are independent.
INT-12 adds explicitly enabled synthetic generic consolidation; arbitrary live
model consolidation and production restore activation remain absent.

Scoped guards reject Internet sockets, DNS, HTTP and production credential factories
for mapped acceptance tests. AF_UNIX is allowed only for mapped local worker IPC,
including INT-19 through INT-25 connected paths. The INT-25 spawned inference and
research workers explicitly install the same guards. Other process fixtures have
their own stated scope. These application-test guards do not establish OS network
isolation; no live provider or Threads requests are part of this gate.

## INT-25 consolidated offline composition

The [consolidated acceptance record](int25-consolidated-acceptance.md) maps INT-22
path-security checks, INT-23 connected and control checks, and INT-24 public runtime,
approval, accounting, uncertainty and recovery checks to exact collected pytest IDs.
The new connected regression reopens a private WSGI runtime after approved public
research and checks durable reply replay without another inference or research call.
It then obtains a separate public-worker draft, applies exact local approval, and
dispatches through fake Threads HTTP. A private canary is absent from the public
research request, public SDK input and fake Threads request. The same application
OwnerStore Stop blocks subsequent private and public work. This is synthetic
composition evidence, not a normative scenario or complete MVP acceptance claim.

Adversarial mappings include Russian, English, mixed-script, encoded and multi-turn
private/public input, delayed private memory, shared/mission poisoning and hostile
worker output. Unique source and payload canaries must remain outside unauthorized
retrieval/context/effects. Permissive H screens and M assessment fixtures cannot
grant G authority. There is no claim of full semantic injection immunity.

## Recorded behavior

The versioned [frozen cases](../acceptance/behavior-cases.json) now contain 24
synthetic cases and 53 recorded outputs, with explicit review dimensions covering all
21 applicable non-G rubric dimensions. They include acceptable and counterexample
answers, plus five borderline answers. The existing BehavioralEvaluationHarness emits bound
artifact hashes and separate G/H/M observations. Russian checks use the actual H gate;
G fixture checks screen decisions/forbidden output and never replace application
privacy/authorization oracles. M scores are recorded **assessment fixtures** supplied
by the corpus author, not new judge results or human-reviewed model scores.

The report includes counts, categorized failures and score distributions. Negative
controls are intentional; their failures show harness detection, not real-model
quality. Threshold 0.75 is provisional. No real model trials, hidden cases or human
calibration ran. Confidence intervals would be inappropriate for these hand-selected
fixtures. Fluent Russian reviewers must calibrate anchors/thresholds independently;
Stage 0 readiness remains BLOCKED until that requirement is satisfied.

## Owner-control concurrency investigation

The untouched base passed 2,792 tests, with 12,501 statements and 3,804 branches fully
covered on Python 3.12.3 (pytest 8.4.2, pytest-cov 7.0.0, coverage 7.11.0, Ruff 0.12.11).
The baseline completed in 271.63 seconds. Twenty independent unchanged runs of the
owner concurrent-operation test passed; there were no retries or xfails.

A controlled actual SQLite writer commit removes its rollback journal exactly before
OwnerStore's lstat, reproducing a false unavailable/stop read on the old code. The
regression failed before the fix. Twenty independent post-fix runs of the
existing concurrency test and both timing regressions also passed, with no retries. One lstat now treats only FileNotFoundError as an
optional absent sidecar. Unsafe modes/owners/symlinks and other errors still fail
closed. A second controlled lock-wait test commits session revocation before the
waiting mutation proceeds and verifies denial with no stop/audit mutation. No sleeps,
unbounded retries or exception suppression were added. This establishes a concrete
application race and correction; sanitized historical PR 26/27 failures alone cannot
prove this was their exclusive cause. Persistent lock timeout remains fail-closed.

## Separately authorized Stage 1 smoke

Stage 0 blockage does not authorize paid work, but the rollout contract permits a
separately scoped synthetic, read-only smoke after its own review. Before it:

1. Review the existing fixed [inference smoke](openai-worker-stage1-smoke.md) readiness
   and dry run; preserve hard-disabled publishing, peer admission, credential isolation,
   `store=false`, bounded calls/timeouts and current stop/spend checks.
2. Independently verify current provider model/tool/SDK/request/source-annotation
   contracts and telemetry/spend error interpretation. This task performed no live
   provider checks or web-contract verification.
3. Obtain exact operator authorization for the specific bounded synthetic paid call;
   inspect sanitized outcome, restore stop and shut down as the smoke contract requires.
4. Research smoke needs a separately reviewed production issuer/adapter and exact
   authorization; the two fake public worker fixtures are not paid authority.
5. Resolve observed drift before further rollout. Human behavioral calibration,
   reviewed participant enrollment, retention/processor/legal decisions and production
   restore fencing remain prerequisites for their affected stages.

No enrollment, production activation, service deployment, Meta call or real publication
is part of this package. The PR remains open for human merge authorization.

## Tooling classification

The production coverage gate still measures every module under `app`, including
OAuth, migrations and operations modules, with no new exclusions. The exact
`acceptance/{__init__,manifest,plugin,guards,behavior,report,run}.py` files are offline
pytest/evidence tooling, not installable production runtime entrypoints. They cannot
authorize model, control or publishing effects. Their collection, aggregation,
fixture controls, stale-artifact rejection and failure handling have dedicated
tests in `test_stage0_acceptance_evidence.py`; crash fixtures are test-only code.
No production coverage scope or threshold was reduced to accommodate this package.

## INT-11 connected synthetic research

SCN-03 now maps independently authenticated WSGI input through an explicitly
constructed application bridge to the actual worker in a separate interpreter.
Trusted bootstrap grants an exact existing input and an exact public fixture;
the model nominates only its fixture name. Worker-local authorization builds and
screens the fixed public question. No private motivation, history or identity is
serialized into IPC or hosted search. The report retains recorded source views,
uncertainty, `claim_status=unverified` and `semantic_support=not_evaluated`.

Tests distinguish unstarted held intent from conservative dispatch uncertainty,
cover real process exits, consumed worker restart/reissuance, concurrent duplicates,
response loss, renewed-session replay, material-source deletion and old-backup
quarantine erasure. Source eligibility is separate from production restore activation.
The worker database still permits only two fixed operations in its entire lifetime.
There is no automatic inference/research recovery resolver.

Only this synthetic connection is complete. Arbitrary privately motivated briefs,
production authorization/bootstrap, real providers and semantic source-support
evaluation remain unavailable. SCN-11 authenticated synthetic participant privacy
controls have connected WSGI evidence. INT-14 adds the connected synthetic SCN-12
matrix below. Human calibration and operator review still block Stage 0 release
readiness and complete-MVP acceptance.

## INT-14 connected synthetic adversarial matrix

`acceptance/cross_channel_corpus.json` is the versioned five-case matrix. Each
case uses authenticated A/B HTTP input and notice acknowledgement, an actual
AF_UNIX research worker with hostile fake hosted-search annotation, instruction-
bearing generic memory and an omitted-parent derivative, reopened stores and
bounded delayed private retrieval, a closed shared proposal and independent
two-phase mission confirmation. A test-only composition reads the current mission
from the reviewed EventStore and PrivacyReadGate for each bounded Threads
deliberation. The proposal then uses verified polling, journal, the literal draft
catalog, independent SyntheticOperator approval, outbox and simulator. INT-18
separately connects explicitly configured offline scheduler/Threads mission
consumption through ThreadsStage0; this INT-14 composition remains test-only.

The synthetic extractor copies the poison statement from the private memory
snapshot it actually receives and declares no semantic parent. The connected
journey checks the protected contextual edge to that exact poison operation,
its source union and counts before deletion and after reopening storage. A
focused control removes contextual binding at the cognition/store seam: the
SCN-12 provenance assertion fails on the missing edge, then passes with normal
binding. HTTP deletion removes the derivative and dependent reply payloads;
they remain unavailable after restart and current-ledger replay into an older
quarantined backup. A later authenticated conversation sees neither the removed
statement nor its private canary.

The connected G checks cover foreign-context and worker-request canaries, model
input provenance, malicious private/public proposals, rejected shared poison
even with permissive H screening, current mission/version, no unauthorized
dispatch, safe literal dispatch, deletion and dependent reply removal, stale
approval after deletion, restart, old-backup quarantine, and no uncertain
resend. Separate public variants revoke approval, stop operation or suspend spend
after preparation. The simulator records zero network calls. Recorded model
decisions and permissive H results do not establish model resistance, semantic
source understanding or human calibration.

## INT-12 connected synthetic private consolidation

SCN-07 now maps all three persisted generic kinds, two-parent and second-level
consolidation, exact historical source unions, restart conversation context,
source/parent erasure, dependent reply invalidation and old-backup quarantine
replay. Tests also cover competing revisions, immutable replay, bounds, forged
and missing edges, cycles, genuine v6 migration rollback, and guarded interpreter
exits inside and after a consolidation transaction. The new
`protected_reply_generic` edge covers replies that used an earlier descendant
before a parent acquired later revision evidence. Current schema is EventStore
v7; supported historical v4–v6 backups remain exact-schema checked. The extractor
and transport are offline synthetic fixtures. No participant deletion UI, live
model adapter, semantic corroboration, production restore activation or
deployment was exercised.

## Offline human calibration handoff (INT-16)

This is preparation for fluent Russian human review, not completed calibration. Export
uses only local synthetic inputs and makes no provider call:

```bash
python -m acceptance.human_review export --output .artifacts/human-review
```

Give each reviewer an independent copy of `blank-review.json` and the readable
`packet.ru.md`. In the copy, set a short `reviewer_id` (pseudonym is fine),
using 1–64 ASCII letters, digits, periods, underscores or hyphens and starting
with a letter or digit; this keeps the readable report on one line per item. Set
`metadata.reviewed_at` to an ISO 8601 timestamp with timezone, and optionally
`metadata.note`. Score each item/dimension from 0 to 1; leave `score: null` when
unable to judge and set `concerns` to `ambiguous` or `insufficient_context` as
appropriate. The answer, prior conversation, source excerpts and authority state
are in the packet. Do not edit item IDs, versions or hashes. Use `evidence_class:
"synthetic_test_fixture"` for test submissions; it cannot count as release
calibration. The `human` value is a reviewer assertion, not verified identity.
The `stage0-human-packet-v3` export has 64 opaque, reproducibly ordered items and
117 item/dimension assessments. Its `uncovered_dimensions` is empty: this means
the review material covers all 21 non-G dimensions, not that any dimension is
calibrated or any model behavior proved. The language-only `russian` case asks only
for `natural_russian`; it has no intellectual-honesty score. Synthetic case context
includes prior sessions, source provenance and independence, matched owner/peer
evidence, a private motivation and candidate public research brief, and an explicitly
**fictional** platform-policy excerpt. It does not state current Meta policy.
Each dimension has Russian scoring anchors. The 0.5 borderline responses invite
reviewer notes when reasonable readings differ. Recorded author scores and control
labels stay outside the reviewer packet. The corpus is
`stage0-recorded-anchors-v2` and context is `stage0-review-context-v2`;
versions, prompts, context, dimensions, guidance, outputs and hashes are bound in
the packet SHA-256. Submissions from older packet versions fail validation.

```bash
python -m acceptance.human_review summarize reviewer-a.json reviewer-b.json \
  --output .artifacts/human-review-summary
```

The command rejects invalid files and writes `summary.json` plus `summary.ru.md`.
A well-formed partial human submission reports `INCOMPLETE`; a fixture-only run
reports `NO_HUMAN_ASSESSMENTS`. Human and synthetic test evidence have separate
reviewer counts, score distributions, missing items, concerns, disagreements and
adjudication items. The `stage0-human-summary-v3` JSON retains the packet SHA-256,
corpus/context/Russian-corpus/rubric versions, reviewer metadata, and every supplied
assessment with item, dimension, score, note and case/context/output hashes. Each
reviewer record has `submission_sha256`: SHA-256 of the parsed submission encoded
as UTF-8 JSON with sorted keys, no extra spaces and non-ASCII characters preserved.
This is reproducible content provenance, not proof of identity or independence.
The readable report lists missing items, disagreements and every reported concern
with reviewer, item, dimension and explanation. Supplied notes are escaped as
literal Markdown text in the readable report and kept verbatim in JSON.

The packet prepares semantic H/M review only. Human interpretation cannot replace
deterministic language screening, privacy authorization or any other G check. The
recorded scores remain author-supplied synthetic assessment fixtures, not human
scores. One packet-bound item received a later qualitative permissibility verdict;
no reviewer has completed scored, dimension-level assessment of this packet.
Even complete human files report
`VALID_REVIEW_EVIDENCE_POLICY_UNRESOLVED` and
`NOT_APPROVED`: the 0.75 thresholds,
reviewer count/independence, sample size, agreement and adjudication policy still
need a separate decision. Review disagreements and threshold proposals, then obtain
an explicit operator threat/residual-risk decision. A failed deterministic G check
remains failed regardless of human scores. The public frozen anchors are not hidden
cases, reviewer provenance is not cryptographic identity proof, and no hand-selected
sample supports a confidence interval or claim about real model trials. The default
acceptance runner does not import review files and remains BLOCKED.

## INT-17 qualitative pilot and narrative policy

The `narrative-policy-cases-v1` synthetic contrast set covers disclosed and undisclosed first-person fiction, a direct history question, factual-authority misuse, and protected private source content. Expected outcomes are M review anchors, not deterministic semantic containment. Existing recorded `behavior-rubric-v1` scores and `stage0-human-packet-v3` identities remain historical evidence under their original policy; they are not silently rescored. A below-0.75 recorded quality control does not prove impermissibility. G authorization, privacy, capability, stop, and spending checks remain independent.

The offline qualitative importer accepts a single strict `qualitative-human-pilot-checkpoint-v1` file, an expected source hash, and an explicit evidence class. Human classification is restricted to the approved pilot's immutable SHA-256; synthetic fixtures declare their class inside the hashed source and cannot be relabeled as human. It preserves the source, all scenario/output identities, timestamps, notes, reviewer provenance, and subsequent product-policy clarification without assigning scores. It never enters the default acceptance runner. Example with the separately supplied local file:

```sh
python -m acceptance.qualitative docs/almost-reasonable-human-pilot-2026-10-16.json \
  --sha256 4bb4bac24d6dd6b4563c305e2f502112e09dea9559d8406cca9a9b078eef360f \
  --evidence-class human --output .artifacts/int17-pilot
```

The raw conversation-derived file and generated import stay untracked. Synthetic importer fixtures must declare `synthetic_test_fixture`. The eight selected responses are six `acceptable` and two `specific_error_reported`, with no supplied explanation for the reported errors. Only the first item retains its original packet binding; the other seven are adapted concrete scenarios with their own hashes. The later explicit permission for first-person fiction and choice of general profile and chat-entry disclosure is a policy clarification, not a revision of the historical verdict. This limited qualitative review does not establish 21-dimension human calibration, numerical threshold approval, operator threat/residual-risk approval, or real-model quality. Public profile wording is a local draft; semantic runtime enforcement of story provenance remains absent. Stage 0 readiness stays BLOCKED.
