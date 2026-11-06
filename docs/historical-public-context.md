# Public development and private history

A public change of position is an epistemic revision, not withdrawal of the
evidence behind an already released private answer. Previously the private reader
compared every historical public receipt with the latest public head. A legitimate
revision therefore hid the answer and removed its private memory influence.

New public exports have immutable revision identities. An existing protected
receipt can resolve the exact stored revision, with its original projection bytes,
dependencies, privacy generation and participant's shared-source permissions.
The current source graph still has to authorize every ancestor. Withdrawal,
erasure, missing content, changed bindings and privacy-generation changes continue
to invalidate dependent answers and memory. Historical availability is never a
new source of action or publication authority.

Unreleased inference retains a stricter check: directly admitted public sources
must still be current before release. Inherited evidence from a previously
released answer may refer to an older live revision. Ordinary new public context
continues to select current heads within the existing source and byte limits.

The public writer transaction maintains `public_state_export_aliases_v1`, an
additive lookup index in its own journal. On first initialization it indexes old
revisions; new revisions are indexed in the same commit transaction. Readers
never create or migrate databases. An alias alone cannot authorize content: its
identity is recomputed and its referenced immutable revision and live roots are
checked again. Legacy receipt bytes and the closed v1 receipt contract remain
unchanged. Both original and reasoned legacy aliases can be resolved after the
writer's backfill.

Old unversioned identities can name different historical revisions. Such answers
remain individually readable, but conflicting protected receipts must not be
combined into one new request. Existing whole-record context admission omits an
incompatible optional record; it never truncates its ancestry. New revision
identities allow different versions to coexist without this collision.

Upgrade the public producer and private consumer as a coordinated compatible
release, preserving the public journal and private histories. Exercise migration,
rollback and source withdrawal on isolated copies first. This change does not
deploy services, change model settings, reset grants or increase budgets.

The regression journeys use actual SQLite storage, private HTTP and local worker
IPC with synthetic model responses. They demonstrate data flow and privacy
boundaries, not real-model quality or completed human acceptance.
