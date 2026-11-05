# Polling confirmed own discussions

The operating window discovers discussion roots from its account's confirmed
top-level outbox posts with `provider_publication_details_verified` evidence.
An absent bootstrap reply ID no longer restricts a running agent to mentions.
Drafts, uncertain publications, replies, and other accounts cannot add roots.

Each discovered root has a separate durable cursor. The least recently attempted
root is selected next; attempting a failing root advances rotation without
advancing its cursor. Restart preserves both positions. Reply and mention slots
alternate when discussions are available, under the existing finite poll-page,
event, timeout, Stop, and operating-window bounds. This does not authorize any
additional model calls or publication effects.

Discovered roots use the flattened `/{thread_id}/conversation` endpoint, which
includes top-level and nested replies in the [official Meta Threads API
collection](https://www.postman.com/meta/threads/request/34203612-13ebe336-0176-4d2b-b208-c36646093139).
Every admitted external reply must have the exact requested root, a valid parent,
and an explicit ownership marker; own replies are skipped. Cursors are bound to
the account, provider user, endpoint, root, and stream. Returned paging URLs are
never followed. The existing explicitly configured legacy root retains its
`/replies` path and cursor when no discovered post exists.

Before each fetch and durable page commit, the service rechecks current polling
controls and the root's verified ownership. Commit keeps cursor compare-and-swap,
event deduplication, pending-queue limits, and the original outbox/reconciliation
workflow. Delivery permits deliberation; it does not require an answer.

The additive Threads schema 10-to-11 migration retains legacy cursors, inbound
events, outbox records, and attempt history. Public and private application
images reading the same Threads store must be upgraded together so their strict
schema readers agree. This PR contains no deployment or live API probe. Fake
Graph responses prove the local data flow, not provider permissions or model
quality in a live conversation.
