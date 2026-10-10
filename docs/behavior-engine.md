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

Each of the authorized-context, mutable-state, and goals sections has both an
item-count and UTF-8 byte bound. A smaller aggregate bound covers all three plus
Core Self, preventing individually valid sections from combining into an
oversized or unexpectedly expensive model request.

The engine performs at most one injected model call and one parse step. It
computes the smaller of the remaining deadline and configured execution budget
and supplies that hard timeout to the application-owned model adapter. The
adapter contract requires enforcing it. A `TimeoutError`, late return, deadline
overrun, or backward wall clock fails closed; a returned proposal is parsed only
after the post-call time check. The included `RecordedBehaviorModel` is
deterministic and has no network/tool path. Emergency stop, known provider-spend
suspension, exhausted limits, expired deadlines, disabled capabilities, and a
channel that cannot support deferral produce explicit non-effect outcomes.
`no_action` is a successful agent choice; it is not reused for `skipped_stop`,
`skipped_spend`, invalid output, or missing capability.

The request deliberately contains no conversational owner-role marker. Owner
status can control capabilities outside this layer, but never changes the
epistemic value of an argument. Capability names in a request are availability
facts only; the model cannot grant itself research, publication, credentials,
spending, tool, or administrative authority. A caller must still pass any core
proposal through the existing `ActionPolicy` and implement separate validators
for future behavior intents.

## Russian H-class output gate

`app.behavior.russian_gate` provides `russian-h-gate-v1` and the frozen
`russian-h-corpus-v1`. The gate applies NFKC compatibility normalization,
rejects hidden control/format characters, removes URLs and code from
conversational-language accounting,
tolerates bounded Latin technical terminology and proper names, and excludes
HTTP(S), FTP, `www`, bare-domain URLs, and code regions before comparing
Cyrillic and foreign prose with no fixed short-reply allowance: unrecognized
foreign words must not outnumber Russian words. Non-Latin letter scripts are converted to bounded
word-equivalent units so Chinese, Arabic, Greek, and similar prose cannot vanish
from the ratio. Foreign quotations remain in the language accounting, so their
Russian framing and explanation must be proportionate to their size. A small
marker set plus non-Russian Cyrillic letters rejects clear Ukrainian,
Belarusian, and Bulgarian proximity cases.

The corpus contains eleven labeled cases: fluent Russian, Russian with English
technical terms, Italian place names, Ukrainian, Belarusian, Bulgarian, English-only,
mixed-script prose, Unicode controls, an explained foreign quotation, and code
with a URL. The current deterministic result matches all eleven frozen labels
(five accepted and six rejected). That is corpus regression evidence, not proof of
language identity. Likely false positives include legitimate Ukrainian or
Belarusian quotations without enough Russian framing and Russian prose with a
large number of uncommon Latin names. Likely false negatives include
grammatically poor Cyrillic text, transliterated foreign prose, and mixed
languages carefully shaped around the thresholds. Naturalness, quotation
necessity, attribution quality, slang interpretation, and semantic language
identity remain H/M calibration questions.

`RussianOutputPolicy` checks the initial draft, performs zero to two injected
regeneration attempts, and then returns an application-owned static Russian
fallback. Expected provider failures during regeneration also terminate at that
fallback; callback contract errors still surface. It never recurses. The H gate
does not authorize an action and cannot override G-class policy.

The gate rejects invisible control/format characters and lone Unicode
surrogates before delivery, so an accepted draft is not carrying a known
non-encodable code point into UTF-8/JSON response and audit paths.

## Autonomous wake-ups

`app.behavior.scheduler` is a deterministic test scheduler, not a daemon. It
uses injected clock, persistence, and deliberation callables. `WakeupPolicy`
supports bounded active hours (including overnight or all-day windows), an
agent-selected cadence, cooldown, duplicate-retention window, maximum iteration
and elapsed-time limits, and per-decision technical rate limits. Active-hour
bounds are deliberately naive wall-clock values interpreted in the injected
clock's timezone; aware `time` values reject at policy construction.

A run filters out future events, coalesces only currently due unprocessed
wake-ups to the newest event, records their IDs, and deliberates at most once.
Future events remain eligible when their time arrives, and a duplicate newest
event cannot starve an older unprocessed due event. The input batch itself is
bounded to 1,024 wake-ups, and there is no catch-up loop. The application-owned
deliberation callback receives the hard elapsed-time limit and must enforce it;
`TimeoutError` and a late return both produce `execution_limit`. Expected
adapter/provider failures (`OSError` and malformed-output `ValueError`) produce
and persist `failed`; programmer contract errors such as `TypeError` continue to
surface for diagnosis.
The same execution-limit outcome applies if the injected clock moves backward
during deliberation; negative elapsed time never admits a proposal.
Duplicate events, inactive hours, cooldown, emergency stop, spend suspension,
rate limiting, invalid decisions, and elapsed-time exhaustion remain distinct
outcomes. Repeated `no_action` is valid and creates no public operation. An
expired duplicate record or cooldown does not permanently starve a later
worthwhile wake-up. Only admitted proposals and admitted `no_action` decisions
enter action-rate history; a rate or execution rejection cannot refresh its own
denial window indefinitely.

The in-memory store and mutable clock exist for deterministic tests. Production
wiring still needs an application-owned durable scheduler repository and actual
policy snapshots. No provider or Threads execution is enabled here.

## Two-phase mission revision and recovery

`app.behavior.mission` uses schema `mission-store-v1` in an isolated atomic JSON
repository rather than changing the shared `EventStore` schema. A proposal
records proposed text, current mission reference, initiating event, supporting
reasons, counterevidence, version identity, time, correlation, model/prompt
versions, a closed purpose category, the initiating source, and the
capability-policy invariant snapshot. The purpose category is a structured
allowlist; the proposed text must equal its application-owned canonical Russian
text, in addition to passing the closed-field screen. Supporting reasons carry
the proposal-specific nuance without becoming active mission instructions.

Only a later `MissionConfirmation` can confirm or reject it. The confirmation
must have a later timestamp, a different event and correlation, independent
evidence, explicit reasons/counterevidence, and exactly the same invariant
snapshot. Each evidence identifier is bound to a typed provenance and source
identifier, and at least one source must differ from the proposal's initiating
source. A required application-owned `EvidenceAuthority` then verifies those
records against protected provenance; event-ID inequality or a model-supplied
source label alone is therefore insufficient. Pending proposals never affect
the active mission. Rejection retains the current mission.
Confirmation creates a new mission version with the former version as its
parent and atomically marks other pending proposals for that former parent as
`superseded`. Resolved or superseded proposals cannot be resolved again. The
capability snapshot is frozen only for an individual pending transition: after
earlier proposals are resolved, a later authenticated operational change may
supply a new snapshot without rewriting history.

Mission text, reasons, and counterevidence use Unicode-normalized, fail-closed
screening for hidden controls, links, instruction/prompt language, credentials,
secrets, budgets, security/deployment controls, shell/Docker, and tool/command
imperatives in Russian and English. The same screen covers the later
confirmation's reasons and counterevidence before durable storage. Every
mission tuple has an item count and aggregate byte bound, and each proposal or
confirmation has a smaller total-record text bound. IDs and version references
use a short ASCII control-identifier grammar, preventing instructions from being
smuggled through evidence or correlation fields. Revision cannot alter Core
Self, operational policy, credentials, provider budgets, deployment stage, tool
permissions, or emergency-stop availability. It may, for example, replace a
Threads-oriented behavioral mission while the separately managed Threads
capability remains unchanged. The lexical screen is deliberately conservative;
semantic adequacy still receives H/M evaluation, while exact capability
invariance remains G.

The active mission document is capped at 256 proposal records. The state
machine refuses a 257th proposal, and repository save/load enforce the same cap,
so valid proposal/rejection cycles cannot make every future parse and atomic
rewrite grow without bound. Existing attributed history is retained; reaching
the cap requires an explicit future retention/archive integration rather than
silent deletion.

The repository writes a replacement file before atomically replacing the active
document. Tests reopen it with a new state-machine instance and confirm
proposal/rejection/history recovery. Final application integration should place
the repository under the deployment's protected durable data directory, add it
to consistent backup/restore and retention procedures, serialize writes through
the application's single-writer discipline, and connect protected evidence IDs
to an `EvidenceAuthority` implementation without rendering private provenance
into ordinary model context.

## Behavioral evaluation artifacts

`app.behavior.evaluation` defines `behavior-rubric-v1`, containing all required
dimensions: Russian naturalness; continuity; independent intellectual
development; honesty; uncertainty; evidence versus interpretation; semantic
citation support; useful privacy-minimized research; single-source skepticism;
disagreement; evidence-driven revision; owner non-sycophancy; trolling response;
reasonable inactivity; privacy-preserving intellectual influence; deterministic
privacy boundaries; semantic platform-policy judgment; mission continuity and
coherent two-phase revision; non-impersonation and no invented human biography;
and no optimization for posting frequency.

An `EvaluationCase` contains frozen synthetic prompts and one or more recorded
outputs together with the generating model, prompt-template, and provider
configuration identities. Each output is a separate trial. Typed, named
`BoundAssessor` values keep deterministic, model, and human assessors in separate
dispatch paths; G rules may only select the deterministic path. The harness
records the actual assessor kind/identity, separate SHA-256 identities for the
frozen case input and output, and an artifact identity binding generation
configuration, case, trial, output, rubric dimension, and assessor. It does not
reproduce prompt or output text in observations. `EvaluationReport.to_json()`
emits a stable machine-readable report with per-G/H/M pass/fail counts,
categorized failures, observations, rubric/harness versions, and explicit
limitations.
Recorded outputs must be a non-empty tuple (never a string implicitly treated as
characters/trials). Every below-threshold observation requires at least one
typed failure category, while passing observations cannot carry failure labels.
Optional human calibration receives every observation and records a non-empty
reviewer identity, calibration version, optional Boolean override
recommendation, and note; malformed or provenance-free adjudication rejects.

Only the complete v1 dimension-to-assurance mapping may emit
`behavior-rubric-v1`. Partial or differently classified experimental rubrics
must carry a distinct caller-supplied version, so their reports cannot be
mistaken for complete release evidence. A G dimension can never use a model
assessor in either official or custom rubrics. Every report requires at least
one case and every case requires at least one recorded trial, preventing an
empty official-looking evaluation artifact.

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
