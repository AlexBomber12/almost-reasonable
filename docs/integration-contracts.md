# INT-01 — Integration contracts and Stage 0 evidence

This inventory describes `origin/main` at `941d6da` (merged PRs #12–#17) and
`tests/test_integration_stage0.py`. It defines application integration obligations;
it does not implement a production coordinator. Existing component guarantees
and future connections are distinguished below. Stage 0 uses synthetic accounts,
temporary SQLite files and fake transports only. It grants no enrollment,
research, shared-memory write, real inference, or publication permission.

## Component ownership and dependencies

| Owner | Existing concrete interface | Dependencies and integration boundary |
| --- | --- | --- |
| `app/core` | `EventStore.append_private_input/retrieve_private_events`, `PrivateInputEvent`, `TrustedPrincipalContext`, `PrivateContextAssembler`, `build_model_request`, `parse_agent_proposal`, `ActionPolicy.evaluate/revalidate` | Owns event envelopes, removable private payloads and protected taint; authorized context precedes request serialization. `SelfStateStore` owns versioned private revisions; `ProviderStateStore` owns provider attempt reservation, usage and durable spend suspension in the application database. Core Self is application-approved. |
| `app/private_web` | `AuthStore.authenticate`, `AuthenticatedSession.trusted_context`, `PrivateWebApplication`, `ConversationService`, `InferenceAdapter.observe` | Owns credential/session SQLite, Host/Origin/CSRF admission and private HTTP routing. Conversation depends on EventStore and context assembler. Default inference is disabled; synthetic observation returns no deliverable text. No gateway, consent or owner-control wiring exists. |
| `app/behavior` | `DeliberationEngine`, `BehaviorModel`, `TestScheduler`, `SchedulerStore`, `MissionStateMachine`, `MissionRepository`, `EvidenceAuthority`, `RussianOutputPolicy` | Owns bounded simulated decisions, cadence/coalescing, Russian H output gate and two-phase mission transition. Scheduler storage is in memory; mission JSON persistence is separate. Behavior intents are not core action grants; there is no application dispatcher mapping them to cognition/research/Threads. |
| `app/cognition` | `CognitionEngine.process`, `MemoryExtractor`, `CognitionReadStore`, `MemoryContextSelector`, `AbstractToSharedValidator` | Resolves exact authorized source/evidence events, then validates proposals through existing SelfStateStore. Read/state adapters must use the same EventStore database. Private interests/beliefs/goals are durable; generic memories and abstraction quarantine/audit are not. Shared commits remain disabled. |
| `app/research` | `build_safe_research_brief`, `ResearchAuthorization`, `ResearchService.run`, `ResearchTransport`, provenance parsing and Russian synthesis | Requires minimized closed briefs plus exact, expiring trusted authorization. Only application-owned public fixtures have an offline issuer. Defaults are disabled; `FakeHostedSearchTransport` is the only provided transport. No production authorization issuer, worker adapter, belief-write or action-policy connection exists. |
| `app/threads` | `ThreadsService`, `ThreadsStore`, `CurrentAuthorizationProvider`, `AccountPolicy`, `ThreadsSimulator` | Owns a separate durable outbox, effect reservations, transitions, inbound cursors and conversation controls. Current approval/privacy snapshots are rechecked at effect boundaries. Simulator approval is test authority only; no Meta transport or integrated privacy-ledger provider exists. |
| `app/privacy` | `TrustedPrivacyContext`, `ConsentService`, `PrivacyLedger`, `DeletionCoordinator`, `ErasureAdapter`, `create_sqlite_backup`, `QuarantinedRestore`, `RestoreAdapter`, `NoticeService` | Owns an independent protected ledger, exact structured consent/revocation, pending erasure intent, notice acknowledgments and quarantined restore eligibility. Application erasure/restore adapters, web control ingress and database activation are missing. A successful fake erasure report does not prove EventStore deletion. |
| `app/trusted` | `TrustedOpenAIGateway.submit`, `OpenAITransport`, `FakeOpenAITransport`, worker protocol/server and credential-file transport builder | Owns provider admission, bounded retries, sanitization and untrusted proposal validation. Existing AF_UNIX worker separates the protocol/process; Stage 0 tests here call the gateway directly with a fake. Credential mounting, OS identities and egress are deployment obligations, not harness guarantees. |

## Authenticated input and trusted context

Private web authenticates the server-held session before reading a message body,
persisting private content, retrieving context or observing inference. Host,
Origin and session-bound CSRF checks remain mandatory. Only
`AuthenticatedSession.trusted_context` supplies conversational identity; extra
JSON identity/privilege fields fail closed. Account disablement, expiry and
revocation deny subsequent input. Constructing a trusted dataclass is not
proof of authentication. Chat, stored memory, public input and model output are
untrusted data, including owner claims.

A participant session does not create `TrustedPrivacyContext` with `CONTROL`
origin or issue Threads approvals. Future control ingress must independently
authenticate structured operations and prevent replay. Owner status never
broadens conversation retrieval or adds epistemic priority.

## Correlation and durable identity

Correlation IDs link bounded technical evidence; they confer no authority and
must not contain private text, participant identifiers or secrets. Durable
operation identity belongs to the component performing the operation:

- Private input deduplicates `(principal_id, authenticated_source_ref,
  source_message_id)`. The source reference is account-identity based, so session
  rotation preserves identity. A retry with changed content returns duplicate
  and preserves the original payload; it does not perform a content update.
- Conversation creates opaque event/payload/correlation IDs. Duplicates return
  no original event ID or inference result. Context assembly and observation
  run only after a newly committed input; a post-commit observation failure is
  not replayed on duplicate submission.
- Gateway `operation_id` reserves durable attempts through ProviderStateStore;
  retries use bounded attempt identities. Reusing a completed/reserved operation
  does not authorize another paid request or replay a completed model result.
- Cognition proposal operation identity and expected revisions pass through
  SelfStateStore; incompatible reuse fails closed. Threads operation identity
  binds immutable outbox content/account/type/parent, independently of likely
  duplicate fingerprints. Privacy deletion IDs bind durable intent and retry.

**Future:** the application needs a recoverable linkage from accepted input to
its downstream operations and results. Neither correlation nor an in-memory
policy grant supplies this linkage. INT-01 adds no EventStore migration or new
event model to invent it. The harness uses explicit synthetic gateway operation
IDs and the persisted input correlation, without claiming production linkage.

## Authorized context assembly

EventStore filters by trusted participant before context selection. The private
assembler then bounds whole messages by count and UTF-8 bytes; Core Self remains
approved and conversation segments retain their untrusted-data label. Model
request building separately validates accounting and bounds escaped JSON.
Authentication references, owner markers, participant IDs and protected taint
have no model-facing fields. A/B isolation applies before relevance ranking.

MemoryContextSelector independently filters by participant/purpose before
ranking, includes untrusted-memory labels and checks complete revision taint.
It is not wired into PrivateContextAssembler. Future assembly must combine
these authorized views with one total budget and version evidence, without
turning any retrieved data into authority or dropping protected source edges.
No shared context is enabled by this contract.

## Proposal, authorization, effect and outcome

ModelRequest → fake gateway → closed proposal validation → ActionPolicy is an
explicit harness composition. ConversationService itself still stops at
`InferenceObservation`; its HTTP `response` remains null. A parsed proposal is
not permission, and `accepted_internal` is not external success.
`dispatchable` currently means only a bound in-memory reply/publication draft
simulation grant. Revalidate the exact proposal, trusted route and current
capability state before any simulated sink. Switching `simulation_only` off
fails closed. An action grant is not Threads approval, privacy consent,
research authorization or permission to write shared state.

`no_action` is a completed internal decision without a dispatchable grant.
Invalid proposal, unavailable capability, stop/spend skip and provider failure
remain distinct operational outcomes. Behavior has its own richer intent and
outcome vocabulary; no implicit conversion to core proposals is defined.
Future integration must preserve each outcome's meaning instead of turning a
failure into deliberate inactivity or claiming a draft was delivered.

Threads recovery distinguishes proven no-effect failure from uncertain
transmission and confirmed effect. `sending` must recover using durable dispatch
evidence; uncertain effect becomes `unknown`, pauses the account and requires
reconciliation. No blind resend/recreation is permitted. `unknown_abandoned`
retains uncertainty/identity; it does not authorize republication. Fingerprints
are bounded duplicate heuristics, not semantic idempotency proof.

## Privacy, taint and consent

Ordinary derivatives retain private audience and protected provenance; model
claims cannot verify themselves. Cognition authorizes evidence against exactly
exposed scoped events. Abstraction is a separate screened transition and still
returns quarantine with shared commit disabled. A passed H screen never grants
transmission/publication authority. Research requires a minimized brief plus
independent exact authorization; conversation/provenance cannot be copied into
research input. Attribution validates recorded references, not semantic truth.

ConsentService authorizes exact principal/content reference/audience/purpose
and validity under current revocations. Neither chat consent nor an owner/model
claim substitutes for structured control evidence. Threads' current privacy
snapshot must eventually be backed by this ledger and exact content binding;
its simulator boolean is not such integration. Deletion must invalidate every
materially tainted derivative and duplicate/index residue through a real
application adapter. That complete adapter is currently missing.

## Stop, spending and restart

ActionPolicy rechecks stop for actionable proposals; `no_action` remains accepted.
The gateway checks stop before new calls and between retries. Confirmed provider
spend-limit errors durably suspend later paid calls, including after restart;
ambiguous quota/rate errors do not establish confirmed suspension. Research
has injected stop/spend callbacks and a suspension hook; scheduler uses explicit
stop/spend inputs. There is no single authenticated application control store
feeding all components, or integrated authenticated reset UI. Future composition
must provide current trusted state to every call/effect boundary. An in-flight
external call cannot be retroactively revoked.

EventStore/auth/provider state survive reopening. Scheduler state and policy
grants do not supply durable recovery. Cross-database privacy commits are not
atomic: record pending deletion before erasure, reconcile all pending/blocked
entries before participant reads, and keep restores quarantined. Restore must
replay the current ledger and rebuild dependencies before eligibility. A ready
marker is not activation authority; switching databases requires the missing
writer/ledger-generation fence and recoverable activation intent. This harness
reopens component instances; it does not claim a complete startup reconciler,
process-crash recovery at every boundary or a production restore switch.

## Deterministic harness and exact evidence

Run `python -m pytest tests/test_integration_stage0.py`; `scripts/ci.sh` includes
it in the existing full 100% line/branch coverage gate. Every fixture provisions
only synthetic A/B identities in isolated temporary auth/event SQLite files.
Clocks are injected, sleeps/jitter are fixed, and assertions never depend on
random event/session IDs. Restart creates fresh stores/services while retaining
only files and browser cookies. Fake provider calls use Stage 1-shaped settings
solely to exercise the gateway; disabled-default behavior is separately proved.
Network socket construction, connection/DNS and first-party credential factory/
file-reader entry points fail the test if reached. No actual credential is
loaded. This is a deterministic data-flow test, not an OS sandbox or proof
against arbitrary native code. Existing worker process tests remain separate.

The following names refer to exact tests in `tests/test_integration_stage0.py`:

| Evidence | Exact test | Supported invariant |
| --- | --- | --- |
| I1 | `test_authentication_precedes_body_and_private_persistence` | Anonymous/expired/revoked/invalid-CSRF requests leave body unread, history empty and inference/provider work absent. |
| I2 | `test_two_participants_restart_duplicate_and_secretless_provider_context` | Same input ID is independent for A/B; scoped history survives restart; input retries suppress observation; a gateway operation retry after restart makes no second transport call; A provider input excludes B and protected identifiers; HTTP has no model delivery. |
| I3 | `test_post_commit_failure_restart_does_not_repeat_inference` | Durable input survives observation failure; restart/session rotation retry preserves the original without repeating inference. |
| I4 | `test_model_output_cannot_grant_permissions_or_real_effects` | Extra approval field fails closed; disabled draft capability and real-effects mode cannot be overridden by model/chat. |
| I5 | `test_no_action_is_success_and_provider_failure_is_not_inactivity` | Completed inactivity differs from failed provider authentication. |
| I6 | `test_stop_disabled_gateway_and_durable_spend_suspension` | Disabled/stop states contact no transport; confirmed spend suspension survives restart and prevents further calls. |
| I7 | `test_policy_rechecks_stop_and_route_before_simulated_effect` | Current stop or A→B route substitution invalidates a previously accepted grant. |
| I8 | `test_web_event_to_cognition_is_scoped_and_durable` | Actual authenticated web event feeds cognition; B cannot expose A to extractor; private interest revision survives restart. |

## SCN-01–SCN-12 traceability and gaps

Statuses apply to the full journeys in [MVP acceptance](mvp-acceptance.md), not
individual modules. **Implemented** requires the entire applicable integrated
journey; **partially implemented** has an executable connected slice but missing
acceptance; **blocked** has no connected journey beyond independent components.
No full scenario is implemented. I1–I8 are the exact test mappings above;
additional test node IDs are supporting component evidence, never E2E completion.

| Scenario | Status | Connected Stage 0 evidence / supporting component test | Missing interface or acceptance |
| --- | --- | --- | --- |
| SCN-01 Private interview | partially implemented | I1, I2, I3, I8 | Deliverable Russian model reply, memory-inclusive conversation context, durable safe shared influence, enrollment controls and behavioral continuity evaluation. |
| SCN-02 Multiuser | partially implemented | I2, I8 | Interleaved durable shared influence/revisions, combined total-budget context and cross-sink privacy tests beyond private events/requests/state. |
| SCN-03 Research | blocked | I4; `tests/test_research_service.py::test_successful_heuristic_screen_does_not_grant_execution_authority` | Conversation→minimized brief→production trusted authorization→worker→Russian synthesis connection and semantic citation evaluation. Core research remains unavailable. |
| SCN-04 Publishing | blocked | I4, I7; `tests/test_threads_outbox.py::test_restart_recovery_uses_durable_dispatch_evidence` | Behavior draft→privacy/Russian gate→current authenticated approval→outbox mapping; real transport/deployment remains outside Stage 0. |
| SCN-05 Disagreement | blocked | `tests/test_behavior_mission.py::test_state_machine_reauthorizes_resolved_evidence_after_restart` | Challenge→deliberation→authorized belief revision or private/public reply; research evidence bridge and non-sycophancy evaluation. |
| SCN-06 Trolling | partially implemented | I3, I4, I5 | Abuse/cooldown and bounded call suppression across private ingress/Threads/behavior, response/termination routing and semantic evaluation. Duplicate input suppression alone is insufficient. |
| SCN-07 Consolidation | partially implemented | I8; `tests/test_privacy_recovery.py::test_old_snapshot_is_reconciled_in_quarantine_before_eligibility` | Generic memory persistence, EventStore-complete erasure adapter, authenticated deletion wiring, index rebuild and combined old-backup restore. Recovery test uses its own adapter. |
| SCN-08 Belief revision | blocked | `tests/test_cognition_pipeline.py::test_persisted_recency_uses_trusted_clock_not_model_timestamp` | Evidence/research/behavior→belief update→later conversation context/restart; deletion reassessment and calibrated belief evaluation. I8 proves interest only. |
| SCN-09 Mission revision | blocked | `tests/test_behavior_mission.py::test_two_phase_confirmation_persists_across_restart` | Deliberation→mission proposal→independent later evidence→confirmation→changed scheduler/Threads behavior. Standalone mission persistence is insufficient. |
| SCN-10 Inactivity | partially implemented | I5, I6; `tests/test_behavior_scheduler.py::test_scheduler_coalesces_missed_wakeups_and_allows_inactivity` | Durable scheduler and application dispatch/outbox metrics connection, later valuable event after downtime/suspension and cadence evaluation. |
| SCN-11 Privacy/deletion | blocked | `tests/test_privacy_deletion.py::test_deletion_writes_pending_first_then_applies_idempotently`; `tests/test_privacy_recovery.py::test_old_snapshot_is_reconciled_in_quarantine_before_eligibility` | Authenticated notice/consent/revoke/delete UI, exact Threads ledger provider, real application erasure/restore adapter, startup read fence and activation transaction. Synthetic reports do not establish real cascade. |
| SCN-12 Injection | partially implemented | I2, I4, I7, I8; `tests/test_cognition_sharing.py::test_uncertain_h_screen_quarantines_and_allowing_malicious_text_grants_no_authority` | Combined hosted-tool, delayed-memory, mission and multilingual/encoded attack journeys across restart/model change. Model output cannot grant permissions in the tested slice; full containment requires each future connection's tests. |

No independent component's unit coverage, simulator approval, heuristic pass,
restore eligibility or internal accepted outcome upgrades these scenario statuses.
