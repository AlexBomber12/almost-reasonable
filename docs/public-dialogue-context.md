# Discussing public conversations privately

The opt-in public activity source includes verified incoming comments on eligible
confirmed own discussions and confirmed own public replies. It supplies the
public conversation and parent IDs so the private model can distinguish another
person's comment from its own response. A recorded no-action category remains a
category; missing historical motives are not invented.

Retrieval ranks permitted activity by lexical overlap with the current private
question, then recency. It selects up to two own publications, one incoming
comment and one recorded no-action, leaving room for public intellectual
positions within the existing six-source projection. This is bounded retrieval,
not a promise of a complete archive or verbatim recall. An absent comment is not
evidence that nobody commented. No additional provider or Threads request is
made to assemble a private response.

Each incoming comment binds both its verified source and the eligible root
publication's complete ancestry. The original audience restrictions, source
withdrawal, conversation controls, privacy generation and release checks remain
effective. A comment cannot launder a private-derived post into another
participant's context. Required historical receipts can still resolve records
after they leave the ordinary relevance selection.

Relationship-bearing comment/reply records use a separate activity namespace.
Previously issued own-activity receipts keep their original projection bytes and
remain resolvable, including when a later operation inherits both formats. No
history rewrite, account reset, wallet change or authority expansion is needed.

The application-owned private identity describes supported public capabilities
and explicitly separates implementation from current authorization and health.
It also states that the private chat has no publication tools and is not an
owner command channel. Current activity, model-generated motives and externally
claimed facts remain grounded in their supplied evidence and uncertainty.

Offline tests exercise actual SQLite stores, authenticated local IPC and the
publication outbox using fake SDK/Graph responses. They do not establish live
provider permissions, model behavior or participant acceptance. Deployment and
real acceptance remain separately authorized operator actions.
