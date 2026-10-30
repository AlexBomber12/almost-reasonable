# Finite autonomous public publication

Stage 7 adds an explicit, default-disabled publication mode to the existing
bounded public operating window. The operator authorizes a finite window and
publication grant once. Within that authority the agent can publish its own
eligible public drafts without per-draft human approval. Choosing no action
remains valid. The grant does not provide additional model calls, research,
credentials, scopes, private-data consent or recovery permission.

## Authority and accounting

`PublicGrantSpec` binds an immutable grant ID to one account, one exact window,
an aware expiry, allowed `post`/`reply` types, a positive finite publication
allowance and a positive finite HTTP-request allowance. The grant cannot outlast
the reviewed window, whose maximum duration is 24 hours. Existing model, polling,
inbound, provider and journal limits remain independent. The same grant ID cannot
be reissued to reset consumption, including after revocation or expiry.

Issuance authenticates the existing local operator and records the issuer.
Autonomous admissions use the distinct `autonomous_publication` purpose and
`grant_admit` audit event. They are not recorded as human per-draft approval.
The exact operation, content, account, source, parent and privacy binding is
reserved durably before authorization or publication. A slot is never refunded.

Every actual publication HTTP request reserves a second durable allowance before
the HTTP client runs. This includes account verification GETs, container POST,
publication POST and readback GET. The ordinary successful transport path uses
five requests for one publication. Polling GETs retain their separate finite
window allowance; they do not silently draw on the publication allowance.
Readiness and `window-plan` make no network calls and issue no grant.

Stop, source authorization, mission, account, scopes, current grant and expiry are
checked again at request boundaries. Observed control replacement invalidates the
grant durably. Expiry observations and the clock watermark persist across
restart. Changing the window configuration cannot extend the same authority.
The transport admission context belongs to the dispatch thread and is removed
afterward, so unrelated supervised operations do not inherit an active grant.

## Conservative recovery

The application records `attempted` before dispatch. It never autonomously retries
an attempted operation, including a failure known to precede publication. This
can waste allowance after a crash but avoids silently repeating an effect. An
unresolved `SENDING` or `UNKNOWN` publication pauses further autonomous work for
the account. Existing separately authenticated reconciliation and recovery
procedures remain necessary. A grant cannot acknowledge duplicate-publication
risk, authorize replacement publication or act as a recovery approval.

Existing supervised windows and per-draft approvals retain their behavior when
no publication grant is selected. Enabling the application flag alone does not
issue a grant or publish pending drafts from other windows or private contexts.

## Explicit operator procedure

The existing protected runtime configuration may set
`enable_autonomous_publication: true`, `autonomous_publication_reviewed: true`,
and the independently reviewed operating-window configuration. Its
`operating_window.publication_grant` must name the matching `public_grant.grant_id`.
The `public_grant` object contains `grant_id`, `account_ref`, `window_id`,
`expires_at`, `max_publications`, `max_http_requests` and `publication_types`.
Use independently reviewed finite values; this document provides no substantive
mission, provider credentials or production grant.

The `app.ops.int24_threads_runtime` commands are:

- `window-plan --config PATH` previews the maximum work without activation.
- `issue-grant --config PATH --activate-live` authenticates and issues the grant.
- `run-window --config PATH --activate-live` uses an already issued matching grant.
- `grant-status ID --config PATH --activate-live` shows durable consumption.
- `revoke-grant ID --config PATH --activate-live` authenticates revocation.

Issuance, live activation, real public posts and deployment require their own
operator authorization. Preparing this PR does not authorize running them.

## Evidence boundary

The offline suite uses the actual local model-worker protocol, scheduler,
SQLite journals, outbox, authorization provider and transport with fake SDK/HTTP
implementations. It checks finite exhaustion, exact bindings, revocation,
expiry, restart and crash behavior, request accounting, Stop and UNKNOWN
quarantine. This is deterministic implementation evidence. It does not establish
real-model judgment, real Meta permissions, two actual participants, production
availability, public launch or complete M1–M6/SCN01–12 acceptance.
