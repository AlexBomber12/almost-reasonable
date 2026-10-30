# Real acceptance evidence protocol

This is an offline, operator-supervised evidence harness for the full behavioral
contract v0.7. It does not call a model, enroll people, post publicly, change grants,
or deploy anything. `acceptance/scenarios.json` remains the sole SCN-01–12 inventory;
its historical Stage 0 baseline is not a claim about current production.

The implementation of this protocol and its schema tests are not real acceptance.
Until separately authorized observations have been collected and reviewed, all
missing requirements remain `NOT_RUN` and complete MVP remains `BLOCKED`.

## Preregister before observations

Use a clean Linux checkout of the exact reviewed HEAD. Keep operator files outside
the checkout in an owner-controlled directory, mode `0700`, each input file mode
`0400`, owned by the invoking user, with no symlinks or additional hard links.
The CLI uses the existing protected-file reader, not arbitrary model paths.

Create a reviewed runtime manifest with exactly these fields:

```json
{
  "version": "real-acceptance-runtime-v1",
  "configuration_digest": "<SHA-256 of reviewed nonsecret runtime configuration inventory>",
  "model_digest": "<SHA-256 of exact provider/model/configuration inventory>",
  "account_digest": "<SHA-256 of independently checked actual Threads account identity>"
}
```

Replace the descriptive placeholders with lowercase hexadecimal digests. The
configuration inventory must identify actual schemas and prompt/Core Self versions,
private/public workers, common owner Stop store, privacy ledger, routing/audiences,
finite grant limits and enabled capabilities. Provider/model inventory must include
the exact model and hosted-tool configuration. Do not include API keys or tokens.
Source HEAD and digest are measured from the checkout by the CLI. The caller is
responsible for ensuring the protected inventory describes the running instances;
the offline importer cannot attest to a remote process or account.

Before collecting trials, humans approve a versioned calibration document: corpus
version, all rubric dimensions, thresholds/weights, sample count, adjudication,
uncertainty and failure handling. The contract leaves these values unresolved;
this harness does not invent passing thresholds. Include all later requirements
and clauses from each scenario's plan, including requirements spanning time.

```bash
python -m app.ops.real_acceptance plan \
  --runtime /absolute/owner-evidence/runtime.json \
  --expires-at '<aware ISO timestamp, at most 30 days after creation>' \
  --trials '<approved integer from 1 to 100 per scenario>' \
  --calibration-digest '<SHA-256 of reviewed calibration document>' \
  > /absolute/owner-evidence/plan.json
```

Exit `2` for a plan is intentional: preregistration is not acceptance. Secure the
output before importing it. Omitting calibration or trials is allowed for planning,
but cannot produce accepted complete MVP. A plan binds all 12 scenarios, normative
clauses, later-stage requirements, HEAD/source/runtime/model/account digests,
created/expiry times, and preregistered trial IDs. Changing any part requires a
new plan and new observations. A dirty checkout cannot qualify. The CLI checks
HEAD, source bytes and cleanliness again before printing a result.

## Collect only separately authorized observations

Use the existing finite runtime entrypoints and owner controls. This development
task does not authorize paid calls, real participants, public posts, enrollment,
credential changes or production activation. Once those actions are explicitly
authorized, execute the actual connected paths, retaining protected technical
receipts locally. Do not convert SDK fixtures or a deterministic gate into live
observations by changing their label.

Every scenario requires independent real-model trials, human assessment of the
complete preregistered trial set, and calibrated heuristic evaluation. Report
distributions and every unsuccessful trial; do not select only good outputs.
One provider attempt cannot be counted as multiple independent trials, including
across scenarios. A finite sample is evidence of behavior, not a probabilistic
guarantee. The imported verdict is an owner-reviewed attestation of the rubric;
the importer does not recompute semantic scores from private transcripts.

Additional observations are derived from capability membership in the sole
scenario inventory:

| Capability | Required actual path and evidence kind |
|---|---|
| M1 | `private_identity`: distinct authenticated participants interacting with the same persistent agent; isolation, continuity and access checks. `human_participation`: at least two distinct authenticated subjects **and** distinct authentication sessions, with independently observed human participation. Two accounts or scripted clients alone are insufficient. |
| M2 | `initiative_cadence_inactivity`: finite autonomous wakes and decisions, justified `no_action`, accepted cadence changes, restart and cancellation; no activity quota. |
| M3 | `hosted_research`: actual authorized isolated hosted `web_search`, multilingual sources, Russian conclusions, citations checked semantically by humans, durable evidence influence and withdrawal. No first-party arbitrary URL fetcher. |
| M4 | `public_conversation`: authorized publication and ownership/readback on the bound account, real inbound polling, decision and response lifecycle, unknown-outcome recovery without duplicate public effects. |
| M5 | `intellectual_evolution_restore`: continuity across people and public experience, traceable beliefs/interests, persistent research influence, fresh independent mission phase, backup/restore and provenance erasure. Retaining an adequately supported belief or mission is valid; forcing a change is not a success criterion. |
| M6 | `owner_privacy_stop`: separately authenticated owner web inspection, spending/grant controls, native Stop for **all** configured workers, revocation, privacy controls and restore erasure; an SSH CLI alone is insufficient. |

Observation files contain only bounded opaque references; keep actual transcripts,
provider receipts, private identities and semantic assessments in the separately
protected evidence store under its retention/deletion rules. Do not commit them.
An observation has exactly these fields (null is allowed only where stated):

| Field | Meaning |
|---|---|
| `version` | `real-acceptance-evidence-v1` |
| `plan_hash`, `scenario` | Exact plan digest and one `SCN-01`–`SCN-12` |
| `kind` | One capability kind above, `model_trial`, `human_assessment`, or `heuristic_evaluation` |
| `source_class` | `real_model`, `human_observation`, `operator_runtime_observation`, or `synthetic_test_fixture` |
| `outcome`, `observed_at` | `PASS` / `FAIL` / `NOT_RUN`; aware timestamp within the frozen plan window and no later than report time |
| `receipt_refs` | Nonempty unique SHA-256 references to the actual reviewed receipts, at most 128; heuristic evidence must also reference the frozen calibration digest |
| `trial_ids` | One preregistered ID for a model trial; all preregistered IDs for human assessment; empty for other kinds |
| `provider_attempt` | SHA-256 reference identifying one distinct actual provider attempt for a model trial; otherwise null |
| `participants` | For human participation, objects with `subject`, `session` (opaque SHA-256 references), `human_observed` (explicit boolean); otherwise empty |
| `reviewer` | Approved opaque reviewer SHA-256 for human observations; otherwise null |
| `model_digest`, `account_digest` | Matching model digest for model trials / matching account digest for public conversation; otherwise null |

All digests are lowercase 64-character hex; HEAD is 40-character hex. Each evidence
file is at most 64 KiB, the package at most 4 MiB. CLI imports at most 512 evidence
files (the pure evaluator has an additional 4096-item bound). Multiple observations
may reference shared protected receipts; the operator must verify they cover the
specific normative scenario, rather than merely prove an action happened.

## Approve exact imports and evaluate

After inspecting underlying receipts and calibration, create a separate protected
approval with exactly: `version: real-acceptance-approval-v1`, `plan_hash`,
`approved_evidence_sha256` (unique SHA-256 hashes of the **exact raw bytes** of every
CI/evidence file to import, including known failures),
`approved_calibration_sha256`, `reviewer_refs` (approved opaque reviewer digests),
and an aware `expires_at`. Approval authorizes import of attestations only.
It grants no model, publication, spending or rollout authority.

```bash
python -m app.ops.real_acceptance report \
  --runtime /absolute/owner-evidence/runtime.json \
  --plan /absolute/owner-evidence/plan.json \
  --approval /absolute/owner-evidence/approval.json \
  --ci /absolute/owner-evidence/ci-evidence.json \
  --evidence /absolute/owner-evidence/observation-001.json
# Repeat --evidence for the complete approved inventory.
```

CI evidence must be the independently verified, current-HEAD Stage 0 `evidence.json`
artifact: clean source, passing deterministic gate, positive test/coverage counts,
zero skipped/failed tests, full line/branch coverage including OAuth. It contributes
to G only. Its historical baseline and `complete_mvp: BLOCKED` are preserved.

Exit codes: `0` means the imported attestation set satisfies this protocol; `1`
means failed acceptance or invalid input; `2` means incomplete acceptance. Reports
expose G (code/runtime invariants, including observed authentication), H (calibrated
heuristic evidence), M (real-model samples plus human rubric assessment), all
scenario/capability statuses, preregistered trial outcomes/distributions, failed
receipt hashes, nonqualifying hashes and missing approved imports. No private
payload or credential is printed; input errors have a fixed redacted message.

A scoped non-fixture failure remains a failure even if its hash was not approved.
An approved but omitted file blocks acceptance. Synthetic observations never
qualify, even if approved. Missing/expired/stale evidence remains `NOT_RUN`;
executed failures remain `FAIL`. None is silently waived or marked inapplicable.
An importer cannot discover a receipt that was never provided or declared: the
human operator must audit completeness, authentic identities, real execution,
semantic ratings, longitudinal duration and calibration.

Reports explicitly say `evidence_basis: owner-approved attestations` and
`independently_verified_external_truth: false`. Hashes prove import integrity,
not external truth. Even an attested `complete_mvp: PASS` always has
`rollout_authorized: false`. Deployment requires a separate owner decision.
