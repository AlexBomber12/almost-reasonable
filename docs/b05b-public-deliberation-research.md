# B05B bounded public deliberation research

Public research remains disabled unless the reviewed INT-24 configuration sets
`enable_public_research: true` and supplies `public_research_socket`,
`public_research_worker_uid`, `public_research_profile`, and
`public_research_scope`. A verified public mission is required. The worker needs
its separate `--profile` and `--authority-journal` configuration and its
existing Stop, credential, and spend controls. No production profile or live
call is included here.

The application and worker use separate protected profile files with the same
public fields. The purpose is `public-deliberation-catalog-research-v1`, and
both files contain `account_ref` for the configured Threads account. Neither
file contains private principal or authenticated source fields. The profile
uses B05A's canonical question catalog, fixed provider policy, expiry, and
finite aggregate and per-topic limits. The worker journal retains consumed
public account slots across restarts and profile replacements. An operator
must separately review and provision this public authority; an existing
private-conversation profile cannot enable it.

The public inference worker receives only available topic IDs paired with their
canonical questions, plus at most one bounded prior result labelled as
untrusted evidence. It accepts a research proposal containing only an exact
topic ID. Trusted application code resolves that ID and sends only the
canonical question, operation ID, and authorization handle to the research
worker. No public message text, rationale, mission text, or private context is
sent to the search provider. One deliberation permits at most one operation;
there is no immediate research-to-model loop.

The public runtime journal records a stable research ID and a started state
before worker contact. Lost responses and interrupted started operations are
uncertain and are never resent automatically. A completed report is kept only
when current authority, source, mission, Stop, spend, and control generation
still permit retention. Evidence remains `claim_status=unverified` and
`semantic_support=not_evaluated`; citation metadata is structural provenance,
not fact verification. Research creates no Threads draft. Any later reply or
publication still needs its independent approval and outbox path. Research and
no action remain available when the mission excludes optional publication.

Offline tests use fake public inference and research SDKs. They cover wakeup and
public message decisions, canonical provider payloads, authority mismatch,
quota durability, Stop and spend changes, lost results, and later evidence
exposure. `complete_mvp=BLOCKED`: semantic source support, belief integration,
and live evaluation remain unfinished.
