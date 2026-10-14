# INT-06 — Trusted synthetic research worker

`app/trusted/research_worker.py` connects the existing Research domain through a
separate `ResearchWorkerServer` AF_UNIX endpoint. It reuses the inference
server's socket ownership/mode checks, Linux `SO_PEERCRED` authentication,
connection/frame deadlines, bounded framing, and single active-handler limit.
It replaces only the handler on this separate server. `openai-worker-v1`/`infer`,
the inference executable, and the fixed Stage 1 smoke remain unchanged. No
Compose, deployment, credential or core persistence files change.

## Closed operation and local authorization

Trusted application bootstrap creates a `ResearchWorkerBoundary` with an exact
`ProviderStateStore` and exact `FakeHostedSearchTransport`. `enabled` defaults to
false and requires an explicit boolean supplied in trusted code. Environment
variables cannot activate it. Even when enabled, the only admissible transport
is the fake; there is no credential factory, SDK client, paid adapter, URL
fetcher or generic Responses proxy. Provider settings are fixed by this module:
`external_web_access=false`, one hosted-search tool/call, at most two attempts,
30 seconds per attempt, 70 seconds overall, 1–4 seconds bounded backoff, 1,000
output tokens, and low search context. These are synthetic request-contract
settings, not a statement of live model/tool support.

Bootstrap calls `authorize_fixture(fixture, peer_uid=...)` inside the worker.
Only the two existing `SyntheticPublicResearchFixture` values are accepted.
The worker selects application-owned public question records, runs the existing
closed brief/privacy/injection builder with trusted local screening material,
and invokes the unchanged exact-fixture issuer. It retains the resulting
process-local `ResearchAuthorization` and screened brief. An application wishing
to screen arbitrary candidate input must still use the brief builder; screening
alone cannot create a worker grant.

The returned request contains a random 256-bit lookup handle. Trusted bootstrap
must pass that handle only to its intended application peer; provisioning is a
local interface, not another socket operation. The grant binds that handle to
an exact fixture, peer UID, brief digest/configuration, provider configuration,
and the issuer's existing ten-minute validity interval. A new handle invalidates
the old grant; `revoke_fixture` revokes it locally. IPC does not deserialize an
authorization object, restore an integrity seal, or issue a fresh authorization.
A future application deployment must supply its own reviewed bootstrap/control
channel; this PR adds no deployment or production authority provider.

The only wire request is:

```json
{
  "version": "research-worker-stage0-v1",
  "operation": "synthetic_public_research",
  "fixture": "auroral-substorms-v1",
  "authorization": "<64 lowercase hex characters supplied by trusted bootstrap>"
}
```

Unknown fields, duplicate keys, arbitrary briefs/provenance, participant IDs,
provider parameters, tools, operation names and configuration are rejected.
The inherited frame bound is 1,120 KiB, JSON nesting is bounded to 16 levels, and
request fields are further restricted to the fixed strings and handle above.
Research replies are bounded to 24 KiB, with at most three source views and
16 KiB of main-finding prose. The existing worker configuration bounds connection
lifetime to 320–340 seconds and I/O timeout to at most 10 seconds. One request
is handled per connection; saturation closes additional connections.

## Durable consumption, guards and evidence

`research-stage0:<fixture>` is a fixed operation identity in the existing
provider-state database. Atomic reservation consumes it before execution.
Successful completion, invalid output, terminal errors, uncertain failure or a
process crash never rearm it. Reissuing a handle or restarting with the same
database cannot repeat that fixture's operation. This intentionally bounds the
entire synthetic harness to two operations per database, each with at most two
transport calls. It is not a general recurring-research rate limiter. Do not
replace/delete a database to evade consumption in a future live workflow.

The worker rechecks current grant/expiry, capability, stop and durable suspension
at admission, before each fake call reservation, and after reservation immediately
before transport. ResearchService independently checks expiry, stop, suspension,
retry limits and deadlines. Time spent inside the transport wrapper's admission
checks reduces the passed timeout. Revocation/stop cannot cancel an already
in-flight request. A consumed reservation with no result remains fail-closed;
there is no blind restart/retry recovery.

Provider attempt IDs are `<operation>:1` and `<operation>:2`; operation evidence
uses the unsuffixed identity. `ProviderStateStore.record_attempt` requires the
matching reservation. Provider attempt `completed` means the fake transport
returned a response; the separate operation outcome records provenance,
synthesis and output-gate acceptance/failure. Counting all usage rows is not a
provider-call count because operation rows have distinct identities. No prompt,
brief, handle, participant ID, source ID, response body or exception message is
persisted. Provider/model/configuration, fixed correlation/operation identity,
time/duration, retry count and a closed outcome are recorded.

The existing hosted-search response type supplies no token telemetry. Token
usage, response ID and cost are therefore explicitly unknown (`None`,
`usage_unavailable`), including for operation rows. No billing totals, inferred
usage, price or invoice cap are invented. The adapter records only the two
existing exact confirmed spend-limit codes through the public atomic
`record_attempt(..., confirmed_spend_limit_code=..., require_reservation=True)`
interface; this durably suspends subsequent inference/research calls sharing the
database. Generic 429/billing-quota or a self-declared `spend_limit_confirmed`
string do not establish suspension. Persistence failure latches this worker
closed; reservations remain consumed when outcome evidence cannot be written.
No core migration or new persistence API is required.

## Untrusted result and source attribution

ResearchService parses the bounded provider search items and source annotations.
Only valid in-message citation offsets/title/HTTPS URLs linked to a consulted
provider URL receive `recorded` status. Missing, malformed, unsupported or
mismatched references retain explicit downgraded statuses. Source IDs, URLs,
titles and prose are screened against trusted local private canaries before
release; IPC never accepts another participant's source records or seals.

The worker builds an existing typed `ResearchSynthesisDraft`/`ResearchSynthesis`
and applies the shared RussianOutputPolicy with zero regeneration calls.
Non-Russian prose produces `output_gate_rejected`, with a fixed Russian failure
message. Invalid/oversized provenance, privacy/injection screening or synthesis
produces a typed failure with no report. Errors never echo the supplied text.

A completed report separates main finding, application-owned uncertainty,
`claim_status=unverified`, `semantic_support=not_evaluated`, and bounded source
views (`reference`, `url`, `status`, `metadata_validated`). Source views are
untrusted JSON data; decoding cannot recreate `SourceRecord` authority or
independent verification. Structurally validated provider references are not
verified factual claims. This conservative integration does not infer which
prose claims the citations support. No result can write beliefs, grants,
permissions, policy, memory or publication; later cognition requires its own
independent evidence and authorization.

## Evidence and separately approved Stage 1 work

`tests/test_research_worker.py` exercises actual AF_UNIX framing/peer admission,
a spawned worker process whose authorization is created locally, default disablement,
forged/missing/expired/mismatched/revoked/replayed handles, closed input/output
schemas, source annotations, private/source-ID canaries, Russian output gating,
pre-call races, bounded failures, durable suspension, consumed crash reservations
and persistence failures. Socket/credential entrypoint guards prove the synthetic
flow does not construct Internet sockets or access credentials. Existing brief,
authorization, service, provenance and synthesis suites cover disguised
identifiers and component bounds. This is deterministic integration evidence,
not proof of a deployed OS sandbox or semantic factual correctness.

A separately reviewed and explicitly authorized Stage 1 research change must:

1. Define an authenticated production issuer based on independent public-source
   evidence, exact approved purpose/brief/configuration, expiry, revocation,
   durable replay policy and audit evidence. Production issuance remains absent;
   no public constructor or `approved=true` field may substitute for that policy.
2. Define a closed credential-holding research adapter and operator activation
   distinct from the unchanged fixed inference smoke. Preserve peer admission,
   secretless minimized input, stop/current approval checks, durable reservation,
   bounded retry/rate/deadline/tool limits and confirmed-spend suspension.
3. Independently verify access to the exact approved model and hosted
   `web_search` combination, `external_web_access`/context-size support, call and
   output limits, `tool_choice`, `include`, and SDK version/serialization.
   Serialize only the existing builder's fixed `to_sdk_kwargs`; never accept a
   client-supplied kwargs dictionary. Keep SDK automatic retries disabled.
4. Normalize the real SDK `model_dump(mode="json")` result into the existing
   modeled hosted-search response while retaining `web_search_call.action.sources`
   and assistant `output_text.annotations`. Test actual `url_citation` title,
   URL and character offsets, absent/unlisted annotations, search/open/find
   variants, and any SDK/provider schema differences with synthetic serialization
   fixtures before a paid call. Add reviewed bounded token/response telemetry
   without copying raw provider output or pretending missing usage is known.
5. Verify protected worker-only credentials, current project/organization scope,
   enforced spend policy, model/tool availability, egress restriction and privacy
   H/M evidence independently. Obtain a separate exact human approval for any
   credentialed test, execute only its approved bounded synthetic operation,
   inspect sanitized results/source annotations, restore stop and shut it down.

This PR performs none of those credentialed steps, loads no real key, runs no
live smoke, changes no deployment, and grants no live research authority. Refer
to [the inference smoke procedure](openai-worker-stage1-smoke.md) for the existing
separation of readiness, offline dry run and explicit live approval; its fixed
request and activation cannot be reused as research approval.

## INT-11 application consumer

The explicit [private runtime bridge](private-conversation-runtime.md#int-11-opt-in-private-research)
now consumes this unchanged closed request protocol. Application authority binds one
exact authenticated private input independently of the worker's local handle and
screened public brief. The worker rechecks current local grant/stop/spend after the
transport returns, before building a report. Application result storage/release
performs its own current checks and retains the report only in the existing reply
payload and dependency graph. Lost acknowledgements do not rearm worker operations.
The ordinary runtime is still disabled; this connection supplies no production
issuer, real transport, semantic verification or cognition/publication authority.
