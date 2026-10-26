# B05C claim evidence: design before implementation

The B05A/B05B provider contract returns one generated assistant message, URL
citation annotations, and web search action metadata. `include` requests
`web_search_call.action.sources`. The parser accepts search, open-page and
find-in-page actions, but none supplies independently retrieved page text to
this application. SourceRecord records structural provenance, not entailment.
The existing ResearchSynthesis types already separate facts and interpretations.

Checked official OpenAI documentation on 2026-10-26:
[web search output and sources](https://developers.openai.com/api/docs/guides/tools-web-search).
It documents generated text, citation annotations, and source URL lists. This
implementation assumes no new API capability and adds no retrieval service.

The smallest workable extension is a bounded JSON answer within the existing
hosted-search operation. It contains a Russian main finding, uncertainty,
contradictions, individual claims (observed_fact or interpretation), and evidence
relationships (supports, contradicts, insufficient, unassessed) with Russian
explanations. The model names URLs; trusted code binds them only to valid returned
SourceRecords and uses the existing synthesis builder. Unknown references and
extra authority/quotation/verification fields fail validation. All relationships
are explicitly same-operation model assessments. Page content remains
unavailable and semantic_support remains not_evaluated: neither a citation,
a generated quotation, nor model agreement establishes independent verification.
Paraphrases are requested instead of quotations; arbitrary prose may still
contain unsupported quotations, and no deterministic validator proves semantics.

Legacy plain-text responses and stored reports remain readable as unassessed.
A versioned assessment travels in the existing worker response, durable public
research result, and later untrusted deliberation context. The evidence-only
context bound must grow (with a transactional migration of its existing SQLite
CHECK) to carry whole bounded assessments instead of dropping contradictions.
Ordinary public message and mission context limits stay as they are.
No research-derived field grants authority or changes mission, belief, budget,
Stop, privacy, approval or recovery rules. There are no extra provider operations,
retries or tools; output remains within the configured token budget.

Tests use controlled source fixtures and fake providers. They prove parsing,
reference binding, conservative status labels, persistence, and authority
boundaries. They do not measure real model entailment, translation accuracy,
resistance to all prompt injections, or completeness of extracted claims.

## Connected result contract

`report.assessment` is an optional `claim-evidence-v1` object. It contains
`method` (`same_operation_model` or `unassessed`), `source_content: unavailable`,
a Russian uncertainty explanation, a contradiction list, and claims. Each
claim has `text`, `kind`, `status` (`model_assessed` or `unassessed`), and evidence
items containing `reference`, `relation`, and `explanation`. Every evidence item
inherits the enclosing model-assessment origin and unavailable-content limit;
its reference points to a structurally recorded provider citation in this exact
operation. Even `supports` is a model-reported relationship, not a verified fact.
Interpretation remains a separate claim kind regardless of citation presence.

The hosted provider adapter requests this answer in its existing call; the
sealed brief still contains only the authorized canonical public question.
The synthetic research adapter retains its existing contract. The public
worker validates source binding and every Russian prose field before returning
an assessment. Both the public runtime and the existing private research
renderer retain it as untrusted data. Missing assessments in historical reports
remain unassessed; later public inference explicitly receives that rule.

There are at most six claims, six contradiction explanations, and three
evidence items per claim. Structured prose fields are at most 2 KB (the main
finding is at most 4 KB), and the complete assessment is at most 12 KB. The
existing response limit remains 24 KB. Durable/public-inference evidence grows
from 2 KB to 24 KB, and the public behavior frame from 32 KB to 64 KB; public
message and mission limits remain 2 KB. The SQLite upgrade copies existing
rows transactionally and preserves consumed/uncertain operations. Oversized or
malformed output fails closed without truncating disagreements or retrying.

The existing output-token allowance is unchanged. More detailed output can
exhaust it, in which case incomplete or malformed answers are unavailable.
Normal input/output token usage is recorded by the existing worker accounting.
There is no added provider operation or evaluator charge, and no paid call,
live Threads call, merge or deployment is part of this implementation.

Local validation: `scripts/ci.sh` passed all 5,349 tests with 100% line and
branch coverage, lint and formatting checks. The run used four pytest workers
and a short task-specific temporary directory to stay within the existing
Unix socket path limit. All tests and coverage requirements remained enabled.
