# Keeping useful context within complete provenance bounds

The serialized model context and its protected dependency graph have independent
bounds. Fitting text into the prompt does not establish that the full graph fits:
a selected memory can have many roots, and a past released reply or fictional
episode can carry research, shared and earlier-public dependencies.

The private conversation assembler now asks the trusted runtime to admit each
complete optional memory, episode or history exchange before adding it. Shared
assembly uses the same admission check. It counts current-context identity,
all event roots, selected creators and their inherited intellectual dependencies,
historical public receipts, and shared/generic/narrative operation groups.
Standalone fictional episodes retain their own lineage without inventing a reply
creator. The callback is application code; model output cannot relax it.

When a public source is configured, admission reserves six dependency entries and
8 KiB of protected metadata for optional public experience. Final intellectual
attachment still checks actual complete ancestry, record bytes and operation
limits. Large optional records are omitted whole. No text or dependency chain is
truncated, and the existing final fail-closed checks remain in place.
Creator and shared-root metadata are cached only within the caller's single
SQLite transaction, avoiding repeated ancestry reads for overlapping candidates.
No cache survives into another request or replaces live source authorization.

This avoids the reproduced case where 100 event roots fit the memory assembler
but the context's own identity made the intellectual snapshot overflow and drop
every memory. Useful ranked memories now survive with their full roots and
permitted public experience. Omission changes this turn's context only; it does
not delete stored memories or promise verbatim recall of all older messages.

Offline regressions use persisted synthetic memories with 99 distinct roots,
with and without a public source. They assert useful partial retention, exact
admitted ancestry, common public experience and isolation of the other private
participant. Existing narrative, research, deletion and shared-context tests
remain required. This is structural continuity evidence, not real-model recall
or semantic acceptance.
