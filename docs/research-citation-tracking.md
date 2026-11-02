# Hosted search citation tracking

A live hosted-search check exposed a mismatch between a model's exact source
URL and the provider citation annotation decorated with `utm_source=openai`.
The research boundary previously rejected that otherwise identical source.

Assessment links may now resolve to one uniquely recorded source after removing
only that exact query component from the recorded annotation URL. The original
annotation URL and source reference remain in the report. Scheme, host, path,
fragment, remaining query bytes and query order must still match. Other tracking
parameters are not discarded. Ambiguous aliases fail closed; exact recorded
URLs keep their original binding. The prose URL gate recognizes the same
provider alias while retaining its punctuation and unrecorded-link checks.

This is structural identity matching, not an independent source-content check.
Claim assessments remain model judgments, `claim_status=unverified` and
`semantic_support=not_evaluated`. No tool, spending, publication, mission or
retry authority changes. Regression tests use synthetic sources and do not
represent live-model acceptance.

The public behavior prompt also states which nullable fields belong to each
action. In particular, a no-action explanation belongs in its closed reason
category rather than `content`, and mission fields must remain null for other
decisions. A live Sol diagnostic had filled `content` on a `no_action` response.
The existing validator continues rejecting that combination without retrying.
The prompt also distinguishes an agent-generated question from the literal
`agent-public-question` capability marker. Neither change forces research or
publication when the agent chooses no action.
