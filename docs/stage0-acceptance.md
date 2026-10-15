# INT-10 Stage 0 acceptance and release evidence

Stage 0 readiness is **BLOCKED**. The acceptance package executes all 12 scenario
mappings and distinguishes connected synthetic checks from remaining requirements.
Missing complete cross-channel attack
coverage and fluent-human calibration prevent Stage 0 acceptance. Complete
MVP acceptance is also BLOCKED. Synthetic success does not grant rollout authority.

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
later independent evidence and the actual mission adapter; test composition supplies
its current text to subsequent bounded deliberation. Ordinary scheduler/Threads
mission consumption remains absent, and recorded decisions do not prove judgment.

Preserved mapped suites exercise actual dispatch failure classification, unknown and
unknown_abandoned, immutable identities, outbox/inbound acknowledgement, approval
revocation, stop/spend timing, source deletion, held model uncertainty, concurrency,
downtime coalescing and process exits. Process-exit evidence covers private reservation/result/release, deletion intent/
application, scheduler start/result/completion and provider dispatch; other checkpoint exceptions establish
transaction/reopen behavior, not comprehensive OS-process crash coverage. Auth,
event/provider, privacy, owner, scheduler, Threads and journal commits are independent.
INT-12 adds explicitly enabled synthetic generic consolidation; arbitrary live
model consolidation and production restore activation remain absent.

Scoped guards reject Internet sockets, DNS, HTTP and production credential factories
for mapped acceptance tests. Only mapped research-worker and INT-11 private-bridge AF_UNIX fixtures are permitted.
The new journeys reject sockets/HTTP explicitly and assert simulator network_calls
is zero. The new spawned exit fixtures install their own guards. Existing spawned scheduler
fixtures remain separately scoped. These are application-test guards, not OS egress containment.

Adversarial mappings include Russian, English, mixed-script, encoded and multi-turn
private/public input, delayed private memory, shared/mission poisoning and hostile
worker output. Unique source and payload canaries must remain outside unauthorized
retrieval/context/effects. Permissive H screens and M assessment fixtures cannot
grant G authority. There is no claim of full semantic injection immunity.

## Recorded behavior

The versioned [frozen cases](../acceptance/behavior-cases.json) cover Russian output,
continuity, disagreement, uncertainty, citation support, privacy-preserving influence,
trolling, silence, intellectual honesty and two-phase mission evolution. Each has
acceptable decision classes, forbidden outcomes, rubric anchors, a positive recorded
answer and a negative control. The existing BehavioralEvaluationHarness emits bound
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
catalog, independent SyntheticOperator approval, outbox and simulator. Ordinary
scheduler/Threads mission consumption remains absent.

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
