# Privacy-safe hosted research

This package defines the independent research boundary for Almost Reasonable. It
does not enable participant research, hold credentials, import the OpenAI SDK,
make network calls, update beliefs or goals, or expose a planner-side research
tool. The default configuration is disabled. Tests use only the deterministic
`FakeHostedSearchTransport`.

## Provider contract verified for this implementation

The contract was checked against the official OpenAI documentation on
2026-10-10:

- New Responses API integrations use the hosted `web_search` tool; the older
  `web_search_preview` name is retained only for legacy integrations. A response
  contains `web_search_call` output items and an assistant `message` whose
  `output_text.annotations` contain `url_citation` entries with URL, title, and
  character offsets. The complete consulted URL list is requested with
  `include=["web_search_call.action.sources"]`. See [Web search — OpenAI
  API](https://developers.openai.com/api/docs/guides/tools-web-search).
- `store=false` disables the default 30-day storage of Response objects.
  Conversation objects persist separately, so this contract supplies neither a
  `conversation` nor a `previous_response_id`. See [Conversation state — OpenAI
  API](https://developers.openai.com/api/docs/guides/conversation-state#data-retention-for-model-responses).
- Responses supports `max_output_tokens` and `max_tool_calls`. This package sets
  both from trusted application configuration and exposes only one tool
  definition: hosted `web_search`. It does not expose functions, file search,
  computer use, code execution, MCP, shell, filesystem access, or a first-party
  URL fetcher.

The request is intentionally represented as typed, secretless data for a future
credential-holding worker adapter. Model/tool availability must be rechecked in
that worker's real deployment smoke test. This PR performs no such call.

## Four separate boundaries

This design deliberately separates four concepts that must not be collapsed:

1. **Syntactic validation** admits only the closed typed brief vocabulary and
   applies type, size, and count bounds.
2. **Heuristic privacy screening** detects common identifiers, known private
   material, injection phrases, and some encodings. Passing this screen is not
   evidence that arbitrary text is public or safe to transmit.
3. **Trusted research authorization** is a separate, opaque, integrity-bound,
   short-lived decision for one exact brief and configuration. It must originate
   from application-controlled evidence.
4. **Provider execution capability** remains synthetic in the trusted Stage 0
   worker. Only an exact deterministic fake transport is admitted; a future
   credential-holding research adapter requires a separately reviewed change.

An arbitrary conversation, participant assertion, extracted memory,
model-generated question, request field, or self-declared `public` label can at
most become candidate input to steps 1 and 2. It cannot issue step 3 or obtain
step 4. Missing, invalid, stale, copied, serialized, field-replaced, or
brief/config-mismatched authorization fails closed before provider contact.

There is no authenticated production authorization provider in Stage 0.
Consequently, ordinary runtime briefs cannot be submitted. The only issuer in
this package recognizes a closed set of exact, application-owned public fixtures
for deterministic offline tests. It cannot authorize arbitrary text, and it
does not enable a real transport.

## Privacy-minimized screened brief

`build_safe_research_brief` is the construction path from candidate records to a
screened brief. The historical function name does not imply authorization for
external transmission. Its closed record vocabulary contains:

- a public/general research question;
- a broad topic and research purpose;
- language preferences;
- an optional justified time/recency requirement;
- publicly verifiable reference identifiers;
- a bounded result budget; and
- a trusted research-configuration version.

The builder accepts exact approved record types, rejects unknown types and the
explicit private-payload type, rejects duplicate singleton fields, and applies
per-field and serialized UTF-8 limits. The resulting JSON has no fields for raw
history, participant or authenticated-source identifiers, personal details,
relationship state, private quotations, protected provenance, credentials,
provider headers, arbitrary request parameters, paths, commands, or tools.
Trusted result limits and provider settings are supplied separately and never
accepted from model output. An integrity seal binds every approved field to the
screening decision, so copying a valid brief while replacing any field revokes
its construction integrity. This seal proves only that the local builder ran;
it is not public-research authorization.

The H screen compares only the already-abstracted brief against trusted local
screening material. It checks direct email/phone identifiers, participant
identifiers, private canaries, quotations, sensitive details, recognizable
four-token private overlap, common multilingual prompt-injection phrases,
Unicode normalization/confusables, invisible characters, and decodable Base64
payloads. Screening material is never serialized. A failed screen produces a
classified local decline, with no provider request.

This is bounded defense in depth, not proof of anonymization. It cannot detect
every email or telephone syntax, form/query transformation such as `+` for a
space, paraphrase, rare-fact linkage, cultural identifier, novel encoding, or
semantic inference. Those are documented heuristic limitations outside the
trusted execution boundary, not claims to be solved by an indefinitely
expanding regular expression set. Genuine bypasses of the separate authorization
boundary remain security defects. H/M privacy risks still require evaluation
with the versioned privacy corpus. Ordinary English, Italian, Russian, and
mixed-language academic, scientific, and philosophical questions can pass
screening without thereby gaining execution authority.

## Trusted public-research authorization

`ResearchAuthorization` is frozen, slot-backed, non-representational evidence
sealed with a process-local integrity key. Its seal covers the issuer, digest of
the exact canonical brief, brief configuration version, provider configuration
version, issuance time, and expiry. Issuance and expiry are normalized to an
absolute UTC timeline before the bounded TTL is applied, including across DST
transitions. Authorization is revalidated when a request is built and before
every provider attempt. Direct construction, dataclass replacement,
serialization round-trips, stale timestamps, or reuse with another
brief/configuration do not confer authority.

`authorize_synthetic_public_research` is intentionally not a classifier. It can
issue evidence only when the screened brief exactly matches one named public
fixture owned by the application. This preserves deterministic synthetic
research while leaving production authorization unavailable and fail-closed.

## Hosted-search isolation and bounds

`build_hosted_search_request` first requires current authorization bound to the
exact screened brief and provider configuration. It then creates a sealed
`HostedSearchRequest`; `to_sdk_kwargs()` revalidates that integrity-bound set of
immutable typed fields and produces a fresh fixed Responses request:

- `store=false`, `background=false`, `stream=false`, and no continuation ID;
- exactly one configured tool type, `web_search`;
- `tool_choice="required"` for an approved research operation;
- trusted `external_web_access` and `search_context_size` values;
- bounded output tokens and total hosted tool calls;
- disabled automatic truncation; and
- only the minimized brief as input.

The default config keeps the capability disabled regardless of environment
variables. `ResearchService` also checks emergency stop and persistent spend
suspension before contact and again before every retry. Confirmed spend-limit
errors activate the injected suspension hook. Rate-limit, server, timeout, and
connection failures receive bounded exponential backoff; authentication,
permission, invalid-request, billing-quota, and unknown failures do not retry.
Attempt count, overall time, per-attempt time, search-call count, source-result
count, input bytes, and output bytes all fail closed.

Provider page text stays untrusted evidence. It cannot add tools, change policy,
spending configuration or Core Self, or directly persist beliefs and goals.

## Citation provenance

Provider output is normalized only after all of these relationships validate:

1. the response completed;
2. a bounded set of completed `web_search_call` items is present, including the
   documented `search`, `open_page`, and `find_in_page` action variants;
3. search-call IDs are unique;
4. the response contains exactly one completed assistant output-text message;
5. every sourced citation is an in-message `url_citation` annotation;
6. its offsets, non-empty title, and HTTPS URL are structurally valid; and
7. the annotation URL occurs in the provider-returned consulted-source list.

A syntactically valid URL by itself is never provenance. Missing annotations,
unlisted/forged URLs, unsupported URL schemes, bad offsets, or malformed
publisher/language/date metadata are explicitly downgraded and cannot satisfy a
sourced claim. Provider-supplied optional metadata is recorded only when present
and well-formed; that does not make it independently verified.

An independently verified prior source can be registered only with a concrete,
timezone-stamped `VerificationEvidence` record naming the verification method
and audit artifact. Source records carry an integrity seal over all metadata as
well as an internal construction authority, so direct construction or field
replacement cannot become sourced. No network verification is claimed by this
package.

The three distinct states remain observable:

- an annotation exists;
- its structure and provider-source relationship were validated; and
- semantic claim support is `not_evaluated` until later M evaluation.

## Russian synthesis and later cognition

`ResearchSynthesis` separates the main finding, observed-fact claims,
interpretations, claim-to-source links, uncertainty, contradictions and
counterevidence, unverified claims, and source statuses. Missing or invalid
evidence references downgrade the associated claim to unverified rather than
inventing support. Even a sourced link retains semantic support as
`not_evaluated`.

Untrusted synthesis drafts are validated before linking or copying. The closed
bounds are 32 claims, 16 contradictions, 32 explicitly unverified claims, 10
evidence references per claim, 16 KiB per synthesis text field, and 64 KiB for
all draft text and evidence-reference bytes combined. The evidence-reference
limit matches the maximum sourced-result budget, while the aggregate limit
matches the provider response-text bound. Derived unverified claims are bounded
by the claim and explicit-unverified limits. Deduplication uses set membership
while retaining the first occurrence order in the returned tuple.

The synthesis is untrusted data and explicitly declares that it still requires
the shared Russian output gate. It is evidence for later cognition integration,
not a command or a direct belief/goal update.

## Trusted Stage 0 worker integration

[Trusted research worker](trusted-research-worker.md) defines the separate
`research-worker-stage0-v1` AF_UNIX endpoint, worker-local fixture authorization,
replay protection, durable technical evidence, output gating, and the remaining
separately approved Stage 1 work. Existing `infer` and fixed Stage 1 inference
smoke contracts are unchanged. Research production issuance and paid transport
remain unavailable.

No private-participant research, deployment change, credential access, or live
provider call is part of this implementation.

### INT-11 synthetic private handoff

The opt-in [private runtime bridge](private-conversation-runtime.md#int-11-opt-in-private-research)
connects an independently authenticated private input to one of the two existing
public auroral fixtures through separate exact application and worker grants.
Private motivation stays in the application; model brief text is never forwarded.
Russian results retain recorded source views and explicit unverified/unevaluated
status in the existing durable private reply, with source deletion and quarantined
old-backup reconciliation. This completes only the demonstrated synthetic SCN-03
connection. Arbitrary private-topic research, production authority/providers and
semantic source-support evaluation remain unavailable; Stage 0 and complete-MVP
readiness retain their unrelated blockers.
