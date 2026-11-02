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
Provider Markdown citations such as `[example.org](https://example.org/source)`
are scanned by their destination when the hostname label matches that
destination (optionally omitting `www.`). The destination must still match a
recorded URL or its narrow provider alias. Misleading hostname labels,
unrecorded destinations and extra bare links remain rejected. This affects
only validation; the report retains its original citation text.
Markdown explicitly delimits its destination, so punctuation inside that
destination must match the recorded URL exactly. Punctuation tolerance applies
only to bare URLs in prose; it cannot change a Markdown path or query value.

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

Two successive live Sol cycles exceeded the legacy five-second scheduler
budget. The public entrypoint and closed inference contract now allow one
attempt for at most 30 seconds. The worker retains a separate ten-second
request-handshake bound, then bounds its response and SDK call by the admitted
inference budget. Admission time and an earlier absolute window deadline are
subtracted rather than extending that budget. Late responses remain uncertain
and are never replayed. Attempts, output tokens, tools, spending and publication
limits are unchanged. Simulated-clock regressions cover a twelve-second reply,
expiry after thirty seconds, and time consumed before contacting the provider.
