# Free bounded mission evolution

Stage 6 adds an explicit `free_missions_enabled=True` application mode alongside
`mission_evolution_enabled=True`. Both default to false. The protected operator
configuration for the existing public entrypoint can set
`enable_public_mission_evolution` and `enable_free_missions`. Its readiness command
remains inactive; live activation and deployment still require separate review.

In this mode the agent chooses its own mission text, reasons and counterevidence.
There is no catalog of substantive missions or topics. The two purpose values
`agent_defined` and `agent_defined_without_threads` distinguish an engaged
direction from a direction that stops optional Threads publishing. They grant no
tools, budget, credentials, operational permissions or publication approval.
Ordinary no-action decisions, rejecting or cancelling a proposal, retaining a
direction, and indefinitely declining optional publishing remain valid outcomes.
Wakeup opportunities and provider capacity remain bounded by existing owner
controls; an opportunity does not require a post or a personality change.

## Two phases and actual context

A proposal persists its model-selected text, supporting reasons, considered
counterevidence, current parent, initiating event, time, model identifier and
prompt version. It leaves the active mission unchanged. A later independently
admitted autonomous wakeup can confirm or reject it. An incoming conversation
cannot directly confirm its own request, including a public claim of ownership.
Cancelling an outstanding proposal also leaves the active direction unchanged.
Stopping optional publishing as a new active direction requires confirmation.

The next actual worker SDK input contains the complete pending text, reasons and
counterevidence in the versioned `free-mission-pending-v1` projection. Confirmation
cannot rewrite that pending text. A confirmed direction survives restart and is
used in later SDK inputs. Pending and active mission contexts carry the complete
protected intellectual dependency union into subsequent intellectual decisions.
Withdrawing a material source blocks reuse and further mission effects pending
authorized reassessment.

Confirmation evidence is labelled `agent_deliberation`: it identifies a distinct
completed provider operation with the correct prompt, operation, parent, event,
control generation and time ordering. Its binding covers the durable decision
payload. The separate invocation must start after the proposal and finish before
its recorded decision time. A stale receipt, altered binding, wrong operation,
earlier invocation or missing provider result cannot supply this evidence.
Transient journal phase is not part of the immutable decision payload.

This establishes application lifecycle and provenance. It does not establish
semantic independence, correct judgment, factual truth or agreement by multiple
humans. The application records the model's reasons and uncertainty, including
an explicitly empty counterevidence list when none was reported. It does not
manufacture positive or contrary reasons. An additional paid research operation
is unnecessary for reconsidering a subjective direction. Hosted research retains
its independent finite authority.

## Bounds and control

Free mission text is at most 2048 UTF-8 bytes. There are at most six reasons and
six counterevidence entries, each at most 512 bytes. The complete pending
projection must fit 8192 bytes before a proposal can be stored. Existing whole
SDK frame, provider output, history and journal limits still apply. Capacity
exhaustion preserves resolution of an existing pending proposal and cannot
replenish proposal capacity.

The existing mission instruction/link/credential screens and the versioned
mutable-state screen apply to these untrusted fields. Russian output checks also
apply before the public mission effect. These are bounded deterministic
heuristics with false positives and false negatives, not semantic safety proofs.
Stored mission data never becomes a privileged system instruction.

Source, mission, mode, Stop, spending and policy invariants are checked throughout
admission and effect. Provider receipts use the configured worker clock; free
decision times are recorded after inference. The public model/usage/revision
metadata distinguishes the free mission prompt from the legacy prompt.

Existing closed mission mode and its serialized records retain their behavior.
The legacy Stage 0/shared mission entrypoints reject free purpose values instead
of interpreting them without a free-text protocol. Disabling free mode cannot
reinterpret a persisted free decision as a legacy authorization. Publishing
continues to use the existing approval boundary until an independently reviewed
finite autonomous grant is implemented and explicitly enabled.

## Validation scope

The offline suite exercises the real local worker protocol, scheduler, journals,
restart and simulated Graph transport. It covers active/quiet directions,
confirmation/rejection/cancellation, complete future context, absence of research
authority, source withdrawal, owner Stop, incoming owner claims, finite capacity,
malformed SDK fields, forged evidence and provider receipts. Bare worker output
cannot authorize a runtime mission effect. Legacy mission and intellectual-state
suites remain part of the full local gate.

These are deterministic integration checks with fake SDKs. Real model quality,
M1–M6/SCN01–12 acceptance, two actual human participants, public activation and
production operation remain unverified. This branch combines pending Stages 3,
4 and 5 before adding Stage 6; none of those PRs is merged by this work.
