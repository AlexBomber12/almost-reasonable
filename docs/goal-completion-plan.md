# Completing the original Almost Reasonable goal

This plan implements the existing behavioral contract v0.7 and its twelve
scenarios. It does not redefine the MVP around whichever components happen to
pass CI. The objective is one persistent, evolving agent across private and
public conversations, with independently scoped private experience. Public
behavior and intellectual continuity are the product; private chat also lets a
participant understand that development.

## User journeys and gaps

| Journey | Required observable result | Gap to close | Contract/scenarios |
|---|---|---|---|
| Two participants meet the same agent | Both can discuss the same permitted public experience and evolving positions, while their personal histories remain separate | A dense private context can discard memories and public state together | M1/M5, SCN-01/02/11 |
| A participant returns to an old subject | A useful, source-bound memory survives transcript eviction and restart, or the agent acknowledges that it cannot recall it | Transcript bounds alone do not provide reliable section budgets or graceful reduction | M1/M5, SCN-01/02/08/11 |
| Someone challenges a post | Every eligible confirmed own discussion can be polled; a verified comment reaches deliberation and a chosen response follows the existing outbox lifecycle | The bootstrap mention-only configuration does not discover later own discussion roots | M2/M4, SCN-04/05/06/10/12 |
| A participant asks what happened publicly | The agent can discuss allowed incoming comments, its own replies, and recorded outcomes; it distinguishes prior reasons from present interpretation | The private activity adapter exposes own posts but omits the discussion and current capability facts | M1/M4/M5, SCN-01/02/04/05 |
| Experience prompts an inquiry | A minimized safe question reaches hosted research; the complete admitted evidence can inform a persisted position, a justified retention, or a recorded no-change | A successful search or an empty revision table alone does not establish intellectual development; diagnose the actual proposal lifecycle | M2/M3/M5, SCN-03/05/07/08/10 |
| The agent changes or retains its direction | Ordinary decisions preserve relevant state; a major mission change has a separate later confirmation; inactivity remains valid | Verify connected source/research/mission paths rather than force activity or state changes | M2/M5, SCN-07/08/10/12 |
| Two people converse naturally | Validated replies arrive without waiting for unrelated auxiliary inference or a full history refresh; concurrency preserves identity and privacy | Auxiliary calls and repeated source validation occupy the reply path; the UI waits for history after POST | M1/M6, SCN-01/02/09 |
| The owner investigates or stops operation | The interface separates decisions, missing inputs, resource denials, failures and unknown effects, and shows public development without exposing private transcripts | Technical process health is insufficient evidence of useful operation | M6, SCN-09/10/11/12 |

## Implementation units

Each unit has a task-specific branch, regression tests first, the unchanged full
`scripts/ci.sh`, 100% first-party Python line/branch coverage, exact remote HEAD
verification, current-HEAD Actions and trusted secret scan, and Codex Review.
Human merge authorization remains required. Independent units can be prepared
while an earlier PR awaits authorization; dependent branches must keep their
base and validation evidence honest.

1. **Context continuity.** Preserve intellectual/public headroom when shared
   context is enabled. Prefer current input, useful private memories and required
   public experience over optional transcript bulk. Preserve all material
   provenance; omit records whose complete dependencies cannot fit. Cover dense
   histories, asymmetric participants, restart, withdrawal, deletion and replay.
2. **Public conversations.** Discover bounded eligible roots only from confirmed
   owned publications. Persist independent per-discussion cursors and scheduling
   progress. Validate account/root/parent binding before persistence, preserve
   pending events and duplicate controls, and keep existing reply authorization,
   uncertain-effect reconciliation, Stop and budget fences. A model never supplies
   an arbitrary fetch destination or expands publication authority.
3. **Common public experience and capability awareness.** Include relevant verified
   public comments and own replies in private retrieval with the same live
   provenance checks. Supply bounded application-owned capability/status facts
   without secrets or administrative authority. Unknown status remains unknown;
   a private conversation does not become an owner command channel.
4. **Conversation latency.** Display a completed, authorized POST response before
   history synchronization. Remove unnecessary auxiliary inference from the
   response path using durable, source-bound maintenance and replay protection;
   preserve revocation and release checks. Validate concurrent participants,
   interruptions, no duplicate paid work, and payload-free stage timing.
5. **Intellectual continuity and owner inspection.** Diagnose absent proposals,
   rejected changes, accepted retention and invalidated state separately. Repair
   actual data-flow/protocol defects, not create artificial beliefs or require a
   revision quota. Expose safe public decision/research/state/mission traces and
   operational denials in the owner interface. Preserve epistemic independence
   from the owner and the existing two-phase mission process.
6. **Connected acceptance.** Connect the repaired journeys to the sole
   SCN-01–12 inventory and real-acceptance protocol. Add honest runnable offline
   regression journeys and participant instructions. Actual provider, human and
   public observations remain distinct evidence, with failed trials retained.

## Completion and operational boundaries

- A component, successful deployment, provider response, or green coverage report
  is not complete-MVP acceptance. All M1–M6 and SCN-01–12 requirements remain.
- Do not promise verbatim recall of every old message. Demonstrate meaningful
  continuity and honest uncertainty with complete authorized source ancestry.
- Do not seed a biography, opinions, interests, or a substantive mission to make
  the agent appear developed. A justified unchanged position or no-action is valid.
- Preserve existing accounts, histories, grants, monthly wallets and unknown
  financial/publication holds. No budget increase or blind replay follows from
  this plan. Existing public and private operating budgets remain separate.
- Do not silently enable private hosted research using public spending authority.
  Capability configuration and any necessary separate authorization must be
  explicit; safe conversational influence on autonomous inquiry remains required.
- No paid operator probe, enrollment, new public test publication, deployment or
  calibrated human verdict is implied by an offline regression test. Use explicit
  authorization for actions outside the already authorized running operation.
- Final completion requires observed connected behavior and human assessments
  under the existing real-acceptance protocol. Missing observations stay NOT_RUN;
  their absence must not be hidden by changing thresholds or labels.

## Current implementation evidence

This document is a plan. Its presence is not evidence that any unit is complete.
Each PR must report its exact implemented scope, validation and remaining gaps.
Runtime incident data and participant information belong in protected operator
evidence, not in repository fixtures or this public planning document.
