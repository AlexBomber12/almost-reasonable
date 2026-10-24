# B03C: bounded participant-private typed memory

The worker can propose one interest, belief or hypothesis, goal or intention
creation or revision for the initiating private input. The application selects
this mode only with `--enable-typed-memory`. It is off by default and can run
alongside generic extraction or generic evolution in the same runtime. The
credential-owning pilot worker separately requires
`--typed-memory-permission /protected/control/typed-memory.json`. This protected
0400 file in a protected 0700 directory has the closed form:

```json
{"version":"private-typed-memory-permission-v1","pilot_id":"synthetic-pilot-id","max_operations":1}
```

The pilot ID must match the current activation and the cap must fit its total
allowance. Generic memory permissions do not grant typed operations. Typed
operations consume the shared memory-operation subset and total pilot allowance;
Stop and both spend controls still apply. These switches do not enable a live
pilot, change deployment, issue an activation or allowance, or make a provider
call by themselves.

`openai-worker-typed-memory-v1` carries the closed
`model-contract-private-typed-v1` projection. It contains the current input,
up to eight whole authorized current typed heads, operation-local opaque handles,
fixed untrusted-data labels, and no principal, source, item, revision, account,
consent or permission identity. The complete serialized projection is capped at
16 KiB; input is capped at 8 KiB. Selection ranks current heads deterministically
by overlap, decision recency and item ID. It omits a whole record when its
serialized fields or complete historical provenance row count cannot fit. Repeated
source-and-role pairs in different revisions still count as separate rows. The provider
output is one `new_memory`, one `revise_memory` of a shown same-type handle, or
`no_change`. Type-specific values remain bounded. The application owns all
identities, timestamps, verification and authorization, and rejects unknown
fields, duplicate JSON keys, invalid values and forged handles.

The source-bound EventStore receipt stores the selected handles and exact heads,
the visible head snapshot and all exposed source IDs before provider I/O. The
separate IPC audit contains only a source/principal binding hash and status. A
lost response is uncertain and is never retransmitted. A received result is
reconciled against its original snapshot and operation identity; it cannot be
retargeted to a newer revision. No-change, rejected and unavailable outcomes
remain distinct in the IPC audit. The existing worker gateway records provider
attempts and usage, including invalid output and interrupted delivery.

EventStore v10 adds a separate `contextual` typed provenance role. New revisions
carry every exposed source as a protected dependency without calling it
supporting evidence. The new input retains its initiating and substantive
supporting or counterevidence role. The existing `SelfStateStore` transaction
validates the complete private source set and writes the typed revision and
contextual rows atomically. Participant statements and model interpretations
remain unverified. A goal is remembered intent and grants no tool or operational
authority. Source deletion invalidates the affected typed item chain and
dependent replies, removes pending receipts, and is replayed into old-backup
quarantine before reads become eligible. Migration and privacy graph checks fail
closed on malformed or cross-principal provenance.

Synthetic fake-SDK tests exercise authenticated HTTP, AF_UNIX transport, the
provider projection and usage ledger, restart, a 2027 to 2028 goal revision,
interest and hypothesis creation and revision, counterevidence, stale and cross-principal
handles, source deletion during a provider attempt, uncertain response,
permission and Stop, receipt erasure, old-backup quarantine and migration.
These tests establish bounded data flow and deterministic policy, not live-model
semantic quality. `complete_mvp=BLOCKED`: shared-memory promotion, independent
research evidence, mission change, autonomy, live calibration, enrollment and
production validation remain separate work. Human merge authorization is still
required; this change does not merge or deploy itself.
