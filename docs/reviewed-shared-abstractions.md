# B04A: locally reviewed general abstractions

This default-off path admits general text outside `SYNTHETIC_ABSTRACT_VALUES` only
after a separate local review. It reuses `AbstractToSharedProposal`, the existing
shared revision tables, protected lineage, bounded context selection, reply edges,
and privacy deletion cascade. It does not add a participant or owner-console route.
The synthetic closed vocabulary and constructor approval path remain unchanged.

## Authority and lifecycle

1. A trusted application supplies an authenticated source principal, exact
   proposal, item and expected revision, complete source IDs, and a permitted
   conversation audience to `SharedIntelligence.quarantine`. The model's proposal
   is data and cannot authorize promotion. Quarantine persists the exact text and
   content-derived binding in removable, source-bound tables. Invalid proposal
   shapes and missing source material fail before persistence.
2. The deterministic H screen rejects obvious identifiers, quotations, private
   markers, and excess source overlap. A failed or uncertain screen cannot be
   approved. This screen is defense in depth; it is not an anonymity guarantee or
   a semantic safety proof. A human reviewer must assess recognizable details,
   source inference, and whether the proposed wording is genuinely general.
3. The OS-authorized local operator inspects the candidate and complete protected
   source material, then explicitly approves or rejects that operation identity.
   The command asks the operator to type the action and operation ID. No candidate
   text is placed in command arguments, immutable audit, or tombstones. A durable
   decision binds the operation, local OS user identity, review version, and exact erasable
   manifest. A second decision ID or changed candidate, source set, audience,
   item, or revision conflicts. A rejected operation remains unavailable.
4. Approval rechecks the current revision, privacy state, source availability,
   current screen version, and exact source consent for every audience. Consent
   uses the existing independently authenticated privacy ledger with
   `shared_abstraction` purpose and the source event as content reference. The
   review never grants consent or a wider audience. Mission evidence remains
   disabled. A new consent revision cannot silently revive a previous approval.
5. Retrieval selects only an active approved head for the authorized audience and
   rechecks that the originally bound consent remains live. The model sees only
   the bounded, whole-record untrusted memory projection; source principals,
   source text, event IDs, consent IDs, review IDs, and manifests stay protected.

## Local commands

Run as an OS account authorized to open the private EventStore and PrivacyLedger
files. The command prints private source material only to the local terminal for
review. Do not redirect or capture that output. The candidate must already have
been quarantined by the trusted application.

```text
python -m app.cognition.review_cli inspect --events EVENTS_DB --ledger LEDGER_DB --operation-id OP_ID
python -m app.cognition.review_cli approve --events EVENTS_DB --ledger LEDGER_DB --operation-id OP_ID --decision-id DECISION_ID
python -m app.cognition.review_cli reject --events EVENTS_DB --ledger LEDGER_DB --operation-id OP_ID --decision-id DECISION_ID
python -m app.cognition.review_cli revoke --events EVENTS_DB --ledger LEDGER_DB --operation-id OP_ID --decision-id REVOCATION_ID
```

The review identity/version and decision identity are operator-controlled tokens.
There is no automatic review or chat-issued approval. Revocation invalidates the
item and removes its payload and dependent reply payloads. Source deletion and
consent revocation use the same protected graph cascade. After a restart, recorded
decisions remain bound to their original operation; no new approval is inferred.

## Recovery and limits

EventStore v11 adds only review identities and protected consent edges. Migration
from v10 is atomic and historical schemas remain validated. The privacy ledger
does not contain a review-revocation journal. During old-backup quarantine,
`rebuild_dependencies` therefore invalidates all reviewed candidates and items, so an
older backup cannot resurrect an authorization revoked after that backup. A fresh
item and review are required after restoring such a snapshot. Synthetic records
retain their existing recovery behavior.

## B04B: private worker suggestions

The private worker can now suggest one new general abstraction, one revision of a
shown eligible abstraction, or `no_change` per admitted turn. Application setup
must explicitly use `--enable-shared-proposals` and one or more
`--shared-proposal-audience` values. The credential-holding worker separately
requires `--shared-proposal-permission`, a protected control file using
`private-shared-proposal-permission-v1`, the active pilot ID, and a bounded
`max_operations`. Both switches default off.

The application sends the initiating participant's input and at most one of that
participant's eligible, previously active abstractions. The worker sees an opaque
handle for that item. The application binds the complete source union, the item
head, the configured audience, and current consent state in an erasable receipt.
A changed snapshot blocks persistence. Intent is recorded before provider contact;
an uncertain result is never blindly retried. A received result can be reconciled
after restart while its original binding is valid. Source deletion erases bound
receipts and candidate payloads. Consent or review revocation conservatively
invalidates all model-derived candidates and erases their receipts. Old-backup
quarantine drops the receipts.

The suggested text enters the existing `SharedIntelligence.quarantine` path. It
remains invisible to other participants until independent local review and exact
current source consent allow promotion. The model does not set source identities,
audience, operation identity, revision, verification, or review approval. The
deterministic screen and local review are still required, including assessment of
recognizable paraphrases and source inference.

Calibrated semantic privacy, real-participant enrollment, live behavioral
evaluation, production restore activation, and complete end-to-end Stop evidence
remain open. The screen does not establish semantic anonymity.
`complete_mvp=BLOCKED`.
