# B05A reviewed public research catalog

This optional authority extends INT-23. The original exact-event approval path in
[`int23-private-research.md`](int23-private-research.md) remains available. Both
paths default to disabled. No HTTP request, participant, or model response creates
authority.

An independent operator reviews a finite set of **canonical public questions**
against the protected context and signs off on a profile. Each topic has an ID,
an exact question, and a positive operation limit. The profile also has an
aggregate limit (1–100), an interval of at most 30 days, a 64-character random
hex profile ID, a separate 64-character random hex authorization handle, an
application deployment scope, the application peer UID, reviewer identity and
time, and the complete fixed provider configuration. The profile version is
`public-research-profile-v1`; purpose is
`private-conversation-public-research-v1`; configuration is
`public-research-v1`. Its `provider_configuration` must equal the JSON object
returned by `app.core.public_research_profile.provider_configuration()`.
Operator review of the catalog is substantive: the parser's identifier, URL,
size, and injection checks are defenses, not a public-data determination.

Two separate protected `0600` files contain the same public fields. The worker
projection has `role: "worker"`. The application projection has
`role: "application"` plus `principal_id` and
`authenticated_source_ref`, binding the profile to one authenticated source.
These private fields never enter the worker file or IPC. The common fields are:

```json
{
  "version": "public-research-profile-v1",
  "role": "worker",
  "purpose": "private-conversation-public-research-v1",
  "configuration": "public-research-v1",
  "provider_configuration": {
    "version": "hosted-search-v1",
    "model": "gpt-6-luna",
    "enabled": true,
    "external_web_access": true,
    "search_context_size": "low",
    "max_output_tokens": 1000,
    "max_search_calls": 1,
    "max_attempts": 1,
    "timeout_seconds": 30.0,
    "overall_timeout_seconds": 70.0,
    "initial_backoff_seconds": 1.0,
    "maximum_backoff_seconds": 4.0
  },
  "profile_id": "<64 random lowercase hex characters>",
  "authorization": "<different 64 random lowercase hex characters>",
  "application_scope": "<operator-defined deployment identifier>",
  "peer_uid": 1001,
  "issued_at": "2030-01-01T00:00:00+00:00",
  "expires_at": "2030-01-02T00:00:00+00:00",
  "reviewer": "<independent operator identity>",
  "reviewed_at": "2030-01-01T00:00:00+00:00",
  "aggregate_limit": 2,
  "topics": [
    {"id": "ocean", "question": "How do deep ocean currents form?", "limit": 1},
    {"id": "aurora", "question": "What causes auroras?", "limit": 1}
  ]
}
```

The example contains placeholders and is not an authority file. Each path and
ancestor must meet the same trusted-directory rules as INT-23. The worker also
needs a separate worker-owned `0600` authority-journal SQLite file in a
worker-owned `0700` directory; create it empty before startup. Its closed
`public-research-journal-v1` schema records profile identity, permanent
revocation, operation IDs, and topic IDs. Keep this file across restarts and
renewals. Losing it requires an explicit new operator decision; never silently
create a replacement in production.

Start the worker with `--profile <worker-file> --authority-journal <journal>`
instead of `--approval`. Start private WSGI with
`--enable-public-research --public-research-socket <socket>
--public-research-worker-uid <uid> --public-research-profile <application-file>
--public-research-scope <deployment-identifier>` instead of the INT-23 approval
argument. The other worker, stop, credential, and persistence requirements in
the INT-23 document still apply. A changed or removed profile file blocks
current work; the worker journals observed revocation. A new profile ID is an
independent operator decision and retires prior IDs in that worker journal.
Reusing an ID with changed fields or restoring a revoked ID is refused.

A model may return a reviewed topic ID or its exact canonical question in the
existing research-choice field. The application resolves the question from the
protected catalog. It rejects unknown choices, edited questions, or appended
private details. Before IPC it checks the current authenticated source, reply
source graph, controls, spend state, and profile. The worker checks its own
profile, UID, stop and spend controls, and durable aggregate and topic slots.
Stored reply evidence includes an opaque fingerprint of the authorizing profile
and topic. A replacement profile cannot release an older profile's reply, even
when it reuses a topic ID. Equivalent casing or Unicode spelling of a canonical
question is removed only from the local private comparison text; any remaining
private variation remains screened.
Only an operation ID, the canonical question, and an opaque authorization
handle cross the AF_UNIX boundary. The provider receives a sealed brief derived
from that exact question. Neither the worker request nor public audit contains
participant IDs, source references, private text, source hashes, or selection
reasons.

The application and worker have independent stores. A started application
operation or reserved worker slot with a lost response is uncertain; it is never
blindly retried. Slot admission precedes provider contact. Output screening,
reply erasure, citations, and `claim_status=unverified` and
`semantic_support=not_evaluated` remain in force. Structural citation metadata
does not prove factual accuracy or support for a claim.

Offline evidence: `tests/test_b05a_profile.py` runs two separately authenticated
turns through WSGI, a fake inference worker, a real AF_UNIX research worker,
and a fake SDK, restarting the research worker between operations. It inspects
provider payloads for canonical questions and exclusion of private canaries.
No production profile, real credential, paid call, Meta call, or deployment is
part of B05A. `complete_mvp=BLOCKED`. Free-form topic authorization,
semantic source support, belief integration, autonomous research, and live
evaluation remain future work.
