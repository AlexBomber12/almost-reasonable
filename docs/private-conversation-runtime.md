# INT-04 private conversation runtime

This is an explicit, offline Stage 0 path. `Stage0ConversationRuntime` composes
`ConversationService`, `PrivacyReadGate`, `PrivateContextAssembler`,
`TrustedOpenAIGateway`, `ProviderStateStore`, `ActionPolicy`,
`RussianOutputPolicy` and `AgentOutputStore`. Its constructor accepts only the
exact `FakeOpenAITransport` class. It cannot construct an SDK client, read
provider credentials, select arbitrary tools, or contact a provider. The existing
CLI and ordinary `ConversationService` still use disabled inference. No listener,
deployment service, scheduler, mission, research or Threads implementation changes.

## Enabling the synthetic fixture

Create isolated synthetic account, application and privacy-ledger SQLite files.
Reconcile the gate before installing the service in the existing WSGI application:

```python
from app.private_web.runtime import Stage0ConversationRuntime
from app.privacy.application import PrivacyReadGate

# store, ledger, fake_transport and synthetic_auth are local fixture components.
gate = PrivacyReadGate(ledger, store)
assert gate.reconcile()
conversations = Stage0ConversationRuntime(
    store, gate, fake_transport,
    emergency_stop=synthetic_owner_store.active,
)
app = PrivateWebApplication(synthetic_auth, conversations, existing_web_config)
```

See `tests/test_private_conversation_http.py` for an executable in-process
construction and authenticated browser round trip. There is no production enable
flag or account enrollment authorization. Gateway settings use the existing
Stage 1 execution switch **inside the exact fake-transport boundary only** to
exercise its actual closed schema and durable reservations. Provider records
are synthetic observations; they establish no invoice, paid call or live model
quality evidence.

## Flow and authority

The WSGI application checks exact Host, session authentication, Origin and CSRF
before reading private POST bodies. JSON accepts only `message_id` and `content`;
participant identity comes from `AuthStore`. After inference, the WSGI boundary
re-authenticates the session and exact route before returning any reply text. A trusted principal dataclass is an
internal routing type, not independent authentication. An owner claim does not
change retrieval scope. Owner Stop is supplied by trusted application code.

Admission, context capture/reservation, pre-inference authorization, result commit,
response release and history reads use the current privacy gate. Its short ledger
write fence checks reconciliation, pending/incomplete deletion, control generation
and application database inode. Reconciliation is explicit; it is never assumed
successful or automatically skipped. Database replacement and new deletion intent
close the path until reconciliation succeeds. No raw store read escapes this fence
in the integrated participant service. The fence is not held during inference.

The assembler receives one participant-scoped event snapshot through the exact
initiating input, excluding later inputs when resuming an unreserved operation.
This ensures a delayed request cannot infer from a newer unrelated message. It preserves existing
Core Self, trust labels, whole-message truncation and byte bounds. All retrieved
source events are recorded conservatively as dependencies, including events
omitted by the assembler. This can invalidate more replies than strictly necessary
but cannot omit an exposed input. The original event must remain in the bounded
100-event retrieval window; an unreserved older operation fails closed. Existing
context is participant-input-only: persisted replies appear in participant history,
but agent replies and private memory/revision text are not model-context roles in
this contract. Shared influence and memory-inclusive conversation remain blockers.

After the fake gateway parses its closed proposal schema, current policy and
controls distinguish a reply, successful `no_action`, invalid proposal, unsupported
action, provider failure and operational skip. Publication drafts and research
remain disabled. A reply passes the Russian heuristic; rejection uses its existing
bounded static Russian fallback with zero extra model calls. The final content
receives a new exact policy grant and storage revalidates the route, proposal,
capabilities and live source graph before persistence. No failure/no-action/skip
is turned into a fabricated conversational reply.

## Durable identities and recovery

The participant plus original `message_id` identifies the logical HTTP submission
across renewed sessions. Its envelope retains the original authenticated source,
event ID, timestamp and correlation. Identical resubmission returns the original
operation; conflicting text is rejected. Deleted envelopes still suppress reuse.
Legacy ambiguous participant/message identities fail closed. Replay never binds
to the latest history event.

Trusted routing derives `reply:<sha256(event_id)>` for the reply operation,
`conversation:<sha256(participant_id)>` for the bounded private conversation route, and
`release:<operation_id>` for release. These remain bounded for every valid legacy
event ID. Only opaque routing identifiers are hashed; no private-content
fingerprint is stored. Model, prompt and approved Core Self versions
and sorted complete source-event dependencies are bound in `ReplyOperation`.
Reservation precedes model execution. A newly created reservation alone owns
inference; racing requests see pending or the committed result. The gateway uses
its existing provider reservation/usage tables and the stable reply operation ID;
there is no second provider-attempt ledger. One fake attempt is allowed per logical
operation, and confirmed spend suspension survives restart.

| Interruption | Retry behavior |
| --- | --- |
| Before input commit | Transaction rolls back; the same request can be admitted. |
| After input commit, before reply reservation | Recover the exact original event and reserve once. |
| After reply reservation, before model call | Preserve pending; no automatic inference replay. |
| After model execution, before reply commit | Preserve pending/uncertainty; never resend the operation. |
| During reply payload transaction | Rollback leaves pending; no inference replay. |
| After durable reply commit, before release | Revalidate current privacy, sources, stop, spend, route, Russian content and delivery capability; then release without inference. |
| After release or lost HTTP response | Return the same released content if it is still authorized; no duplicate model call or release row. |

There is no automatic resolver for uncertain inference. A pending reservation is
an honest operational state, not success or permission to execute a replacement.

## Release lifecycle and HTTP contract

EventStore v5 adds exactly one metadata-only table, `agent_reply_resolutions`,
keyed by the existing reply reservation. It stores one immutable terminal outcome
and, only for `completed`, the unique trusted release ID. It contains no content,
source copies, hashes or provider-attempt state. Migration from exact v1–v4 schemas
is additive and transactional, with a v5 pre-commit interruption point. Unknown
schema objects and orphaned references remain rejected. Genuine v4 backups remain
supported alongside v5 backups. The real quarantine adapter validates and erases
v4 against its own exact schema and graph; it does not initialize EventStore or
upgrade the descriptor-owned quarantine. Live reads/erasure still require v5.

The original v4 draft columns remain `held`. A separate durable completed resolution
is the reviewed **released-for-response** transition; draft existence alone grants
no rendering. `release_reply` revalidates the exact policy grant, current controls,
Russian content and complete live source set in its storage transaction. Release
records and source edges remain internal. History renders only released replies
and rechecks current controls and source availability. Current stop/spend/delivery
suspension hides a previously released reply without claiming it was recalled.

`POST /api/messages` retains the existing JSON contract:

```json
{"message_status":"created","model_state":"completed","response":"Да, я готов обсудить этот вопрос."}
```

`message_status` is `created` or `duplicate`. `model_state` is `pending`,
`completed`, `no_action`, `failed`, `invalid`, `unsupported` or `skipped` in the
synthetic runtime. Only `completed` includes reply content. Ordinary disabled
inference continues returning `unavailable` and null response. HTTP creation uses
201; identical retries use 200; conflicting payload uses a generic 400 error.
Privacy/storage failure returns a payload-free error, never model internals.

`GET /api/history` returns `messages`, each with existing `event_id`, `occurred_at`,
`content` and an additive `role` (`participant` or `agent`). A released reply
immediately follows its exact source input in event timestamp/ID order. HTML
escapes both participant and agent content; JSON remains `application/json`
with `nosniff`, no-store and existing restrictive security headers. Versions,
protected dependency edges, provider results, prompts and configuration are not
serialized to the participant API.

`completed` proves server release, **not confirmed client receipt**. There is no
client acknowledgement protocol. A lost response may be retrieved with the same
request identity. Content already read/released into an in-flight HTTP response
cannot be retroactively recalled after deletion or Stop; subsequent authorized
reads and retries are fenced again.

## Deletion and quarantine

INT-03 erasure still removes all dependent reply payloads and invalidates operation
and source edges in the original transaction, including replies released earlier.
The new resolution rows contain only retained identity/control metadata and grant
nothing when sources or payloads are unavailable. Privacy graph validation checks
resolution/result consistency and exact trusted release identity. Old backups
replay even applied ledger deletions before private reads or quarantine eligibility;
a released reply cannot resurrect through restore or retry. No live database
activation API or Stage 2 privacy claim is introduced.

## Evidence and remaining boundaries

Run `scripts/ci.sh`, or independently run:

```sh
python -m pytest tests/test_private_conversation_runtime.py tests/test_private_conversation_http.py
python -m pytest tests/test_private_reply_release.py tests/test_event_store_v5.py tests/test_privacy_application.py
```

These exercise actual first-party components using synthetic A/B identities,
controlled clocks, temporary SQLite databases and fake transports. Runtime tests
cover exact replay, crash checkpoints, concurrency, Russian fallback, Stop,
spend suspension, deletion before/during/after work and old backups. HTTP tests
cover authentication, request guards, renewed sessions, response loss, HTML/JSON
safety and private canaries. Storage tests independently exercise invalid grants,
route/content/source mismatches, held drafts, immutable decisions and migration
rollback. Network and credential entry points are explicitly forbidden in an
integration test. This is application data-flow evidence, not an OS sandbox.

Production inference, real participant enrollment, consent/deletion UI, encryption
and retention controls, safe live restore activation, shared-memory commits,
research, mission updates, public disclosure, Threads effects and semantic model
behavior evaluation remain unavailable or unvalidated. No complete MVP scenario
is claimed; see the updated scenario traceability in `integration-contracts.md`.
