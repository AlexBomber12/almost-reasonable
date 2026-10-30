# Agent-generated public research questions (PR4)

The optional `public-research-profile-v2` permits questions selected by the agent
without prelisting their substantive text. The existing v1 reviewed catalog and
exact-event approval remain unchanged. A question in intellectual state is still
an intention; it creates no research authority. Production is not activated.

## Finite operator authority

V2 retains the protected application/worker files, profile identity, independent
authorization handle, deployment scope, peer UID, issuance/expiry interval,
reviewer, aggregate limit and complete fixed hosted-search provider configuration.
Aggregate capacity remains1–100, interval at most30days, one hosted search call
and one attempt per admitted research operation. Stop, spend and live profile
checks remain required. Existing worker journal records consumption before
contact; lost/uncertain work stays consumed across restart. Replacing a public
profile does not reset existing account-level consumption.

Replace the v1 `topics` field with this exact policy object:

```json
"question_policy": {
  "mode": "agent_generated_public_questions",
  "max_question_bytes": 512,
  "per_question_limit": 1
}
```

The per-question limit is positive and no greater than aggregate capacity. It
counts exact UTF-8 question identities, not semantic equivalence. The private
purpose remains `private-conversation-public-research-v1`. The new public purpose
is `public-deliberation-autonomous-research-v1`, bound to an explicit account.
The application projection alone carries private principal/source binding;
neither enters the worker projection or provider request. Unknown fields,
arbitrary tools/provider configurations, unbounded policies, role mixing and
version/purpose substitution are rejected.

## Actual consumers and recovery

Private reasoning can propose a new bounded question in the existing research
proposal field. Before IPC, the application screens a minimized research brief
against its actual authorized private source/context union. V2 question text is
never removed from those comparisons merely because the model selected it.
That exemption remains restricted to independently reviewed v1 canonical spans.
Long private tokens and name-shaped tokens are conservatively withheld from new
questions. This H screen has false positives and does not establish semantic
de-identification or anonymity. Real-model privacy/usefulness evaluation remains
separate acceptance work.

Only a passing question acquires an erasable `researchq:` receipt in the existing
protected receipt store. The receipt binds the exact question, profile digest,
authenticated initiating operation and complete selected source/authorization
snapshot. Provider metadata fingerprints only admitted questions, never rejected
private wording. Replaying an earlier result requires its original question/
profile fingerprint and current source authority. Restart does not call the
provider again. Source erasure removes dependent receipts; quarantine checks
their closed representation, routing and dependency snapshot before activation.

Public deliberation receives the trusted `agent-public-question` capability
marker instead of a substantive catalog entry. Only that configured offer lets
the worker return its own question in the existing bounded choice field. The
application pins the offered profile and exact question in its durable intent,
then reserves per-question/aggregate capacity before separate research contact.
No private history or protected provenance is included in hosted-search input.
The report and exact question can enter a later authorized public context as
unverified evidence. Citations/provider annotations do not establish factual
truth, and this PR does not claim completed evidence-driven belief evaluation.

No arbitrary first-party URL fetcher, tool privilege, extra model permission,
autonomous publication authority or implicit budget renewal is added. Existing
worker startup and independently reviewed protected configuration procedures
still apply. Tests use fake providers; actual paid calls or deployment need
separate operator authorization.
