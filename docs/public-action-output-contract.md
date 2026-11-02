# Public action generation contract

The public SDK previously advertised a flat nullable object. Structured Outputs
could accept, for example, `no_action` with both `reason_category` and `content`.
The native parser correctly rejected that mixed action as `provider_error`.
The schema also left question length unbounded despite a 512-byte native limit.
Synthetic counterexamples reproduce these producer/consumer mismatches; old live
errors without stage codes cannot establish which mismatch occurred historically.

The SDK now requests an object with a nested `proposal` union. Each action has
its own branch, with every inactive field required to be null. Mission resolutions
cannot rewrite the proposed purpose or text. Optional same-call intellectual
fields remain outside `proposal`, with their existing validation. A nested union
preserves the object root required by OpenAI Structured Outputs; see the
[official documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

Generated questions have a conservative 128-character cap, which fits 512 UTF-8
bytes even for four-byte Unicode scalars. Generated replies, drafts and mission
text/reason items likewise have conservative character caps within their native
byte bounds. Native safety checks still decide whitespace, semantic restrictions,
catalog membership and authority; the schema cannot establish their acceptance.

Only an exact SDK `proposal` envelope is unwrapped. Its contents go through the
existing local parser, without dropping unknown fields, coercion, salvage,
additional calls or retries. Previously valid flat objects still receive the same
local validation. The application/worker protocol, database schemas, model
selection, Stops, finite grants and consumed budgets are unchanged. Rejected
response text is not retained.

Regression tests use synthetic SDK responses, JSON Schema counterexamples, and
the existing connected Unix IPC runtime. These tests are not live model acceptance.
At the initial post-PR106 observation, the normal live cycle completed with
`no_action`; it did not reproduce the historical failure. The new generation
contract requires its own reviewed rollout and subsequent normal live observation
before claiming that intermittent production failures are resolved.
