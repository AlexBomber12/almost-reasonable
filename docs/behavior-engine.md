# Behavior engine v1

This package implements the bounded behavioral decision layer described by the
v0.7 contract. It is deliberately an effect-free layer: model-like components
return proposals, while existing trusted application policy remains responsible
for authorization and any later integration. Nothing here publishes, browses,
contacts a provider, changes credentials, changes budgets, or changes security
configuration.

It is not the complete autonomous product. Real research, cognition routing,
participant authentication, shared-state application, Threads adapters, and
production scheduler wiring remain separate integration work.

## Deliberation contract

`app.behavior.deliberation` accepts a `DeliberationInput` containing only:

- an application-authorized context snapshot;
- the exact immutable `CoreSelf` value from `app.core.core_self`;
- permitted mutable state and current goals;
- an application-owned capability-availability snapshot;
- a typed trigger and prior-result category;
- a timezone-aware current time/deadline; and
- explicit step, model-call, and elapsed-time bounds.

It emits `DeliberationResult`. Existing core decisions (`reply`, `research`,
`propose_publication`, and `no_action`) retain the existing `AgentProposal`
types. Decisions whose application adapters are not available yet are closed,
typed `BehaviorIntent` values for `remember`, `revise_belief`, `revise_goal`,
`revise_mission`, `defer`, and `terminate_conversation`. Unknown decisions,
unknown fields, malformed values, oversized values, invalid goal states, and
attempts to add administrative fields fail closed.

The engine performs at most one injected model call and one parse step. The
included `RecordedBehaviorModel` is deterministic and has no network/tool path.
Emergency stop, known provider-spend suspension, exhausted limits, expired
deadlines, disabled capabilities, and a channel that cannot support deferral
produce explicit non-effect outcomes. `no_action` is a successful agent choice;
it is not reused for `skipped_stop`, `skipped_spend`, invalid output, or missing
capability.

The request deliberately contains no conversational owner-role marker. Owner
status can control capabilities outside this layer, but never changes the
epistemic value of an argument. Capability names in a request are availability
facts only; the model cannot grant itself research, publication, credentials,
spending, tool, or administrative authority. A caller must still pass any core
proposal through the existing `ActionPolicy` and implement separate validators
for future behavior intents.

## Russian H-class output gate

`app.behavior.russian_gate` provides `russian-h-gate-v1` and the frozen
`russian-h-corpus-v1`. The gate normalizes text, rejects hidden control/format
characters, removes URLs and code from conversational-language accounting,
tolerates bounded Latin technical terminology and proper names, and discounts
quoted or block-quoted foreign material before comparing Cyrillic and Latin
prose. A small marker set rejects clear Ukrainian and Belarusian proximity
cases.

The corpus contains ten labeled cases: fluent Russian, Russian with English
technical terms, Italian place names, Ukrainian, Belarusian, English-only,
mixed-script prose, Unicode controls, an explained foreign quotation, and code
with a URL. The current deterministic result matches all ten frozen labels (five
accepted and five rejected). That is corpus regression evidence, not proof of
language identity. Likely false positives include legitimate Ukrainian or
Belarusian quotations without enough Russian framing and Russian prose with a
large number of uncommon Latin names. Likely false negatives include
grammatically poor Cyrillic text, transliterated foreign prose, and mixed
languages carefully shaped around the thresholds. Naturalness, quotation
necessity, attribution quality, slang interpretation, and semantic language
identity remain H/M calibration questions.

`RussianOutputPolicy` checks the initial draft, performs zero to two injected
regeneration attempts, and then returns an application-owned static Russian
fallback. It never recurses. The H gate does not authorize an action and cannot
override G-class policy.

## Autonomous wake-ups

`app.behavior.scheduler` is a deterministic test scheduler, not a daemon. It
uses injected clock, persistence, and deliberation callables. `WakeupPolicy`
supports bounded active hours (including overnight or all-day windows), an
agent-selected cadence, cooldown, duplicate-retention window, maximum iteration
and elapsed-time limits, and per-decision technical rate limits.

A run coalesces missed wake-ups to the newest event, records all coalesced event
IDs, and deliberates at most once. There is no catch-up loop. Duplicate events,
inactive hours, cooldown, emergency stop, spend suspension, rate limiting,
invalid decisions, and elapsed-time exhaustion remain distinct outcomes.
Repeated `no_action` is valid and creates no public operation. An expired
duplicate record or cooldown does not permanently starve a later worthwhile
wake-up.

The in-memory store and mutable clock exist for deterministic tests. Production
wiring still needs an application-owned durable scheduler repository and actual
policy snapshots. No provider or Threads execution is enabled here.

## Two-phase mission revision and recovery

`app.behavior.mission` uses schema `mission-store-v1` in an isolated atomic JSON
repository rather than changing the shared `EventStore` schema. A proposal
records proposed text, current mission reference, initiating event, supporting
reasons, counterevidence, version identity, time, correlation, model/prompt
versions, and the capability-policy invariant snapshot.

Only a later `MissionConfirmation` can confirm or reject it. The confirmation
must have a later timestamp, a different event and correlation, independent
evidence, explicit reasons/counterevidence, and exactly the same invariant
snapshot. Pending proposals never affect the active mission. Rejection retains
the current mission. Confirmation creates a new mission version with the former
version as its parent. Resolved proposals cannot be resolved again.

Mission fields reject links, administrative/tool instructions, and requests to
change operational controls. Revision cannot alter Core Self, operational
policy, credentials, provider budgets, deployment stage, tool permissions, or
emergency-stop availability. It may, for example, replace a Threads-oriented
behavioral mission while the separately managed Threads capability remains
unchanged.

The repository writes a replacement file before atomically replacing the active
document. Tests reopen it with a new state-machine instance and confirm
proposal/rejection/history recovery. Final application integration should place
the repository under the deployment's protected durable data directory, add it
to consistent backup/restore and retention procedures, serialize writes through
the application's single-writer discipline, and connect protected evidence IDs
without rendering private provenance into ordinary model context.

## Behavioral evaluation artifacts

`app.behavior.evaluation` defines `behavior-rubric-v1`, containing all required
dimensions: Russian naturalness, continuity, honesty, uncertainty, evidence
versus interpretation, disagreement, owner non-sycophancy, trolling response,
reasonable inactivity, mission continuity, evidence-driven revision, no
invented human biography, privacy boundaries, and no optimization for posting
frequency.

An `EvaluationCase` contains frozen synthetic prompts and one or more recorded
outputs. Each output is a separate trial. An injected assessor returns a score
and typed failure categories; the harness records a SHA-256 artifact identity,
not the output itself, in observations. `EvaluationReport.to_json()` emits a
stable machine-readable report with per-G/H/M pass/fail counts, categorized
failures, observations, rubric/harness versions, and explicit limitations.
Optional human calibration receives every observation and records reviewer,
calibration version, optional override recommendation, and note.

G, H, and M results remain separate. The harness rejects a model assessor for a
G-class rule; model agreement can never establish access control. M rules do not
encode one preferred opinion as the correct result. A passing recorded-output
distribution or 100% Python coverage does not prove probabilistic behavioral
quality. Quantitative M thresholds and a representative human-calibration set
remain pre-production decisions.

## Test and safety map

The behavior tests use only recorded responses, injected functions, temporary
files, fake persistence, and an injected clock. They cover:

- owner-role spoof text and closed-field attempts remaining inert data;
- unknown actions and model-provided administrative/tool fields failing closed;
- goal-policy and mission instruction/link injection rejection;
- reasoned Russian disagreement and unchanged owner epistemic weight;
- repeated `no_action` distinct from stop, spend, rate, policy, and failure;
- abusive-input cooldown and conversation termination intents;
- conflicting evidence fields and independent mission confirmation evidence;
- immediate self-confirmation and changed capability snapshots failing;
- restart recovery for pending and resolved mission records;
- wake-up coalescing, duplicate suppression, active hours, cooldown, execution
  bounds, per-action rates, emergency stop, and spend suspension;
- disabled/revoked behavior capability paths;
- all frozen Russian-gate corpus categories and exhausted regeneration; and
- zero live APIs, hosted tools, filesystem capabilities beyond the isolated
  mission repository, or Threads publication.

Privacy authorization, participant isolation, taint propagation, actual belief
application, research-brief minimization, and publication outbox enforcement
remain G-class responsibilities of their owning integration layers. This engine
does not weaken or replace them.
