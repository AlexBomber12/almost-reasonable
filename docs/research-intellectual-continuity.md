# Research and intellectual continuity

Stage 5 makes an admitted research report available as a source for an agent's
next ordinary intellectual decision. It adds no reflection call, polling,
permission, provider setting or external effect. The existing bounded inference
may revise a belief or goal, explicitly retain its fields with a new assessment,
or return `intellectual=null` and leave state unchanged. Research completion does
not force an opinion change.

## Actual inference inputs

Public deliberation already receives one selected complete cached report in
`prior_untrusted_public_evidence`. That exact report now has a selectable opaque
source handle of kind `hosted_research_model_report`. Its short source label
identifies the complete report elsewhere in the same SDK request. The existing
question, uncertainty, contradictions and claim/source relationships stay in the
full report. Historical reports that were not selected do not get handles.

In a worker private conversation, an authorized, completed, server-released
research response can enter the existing bounded conversation transcript. It is
whole untrusted conversational data, including all its rendered assessment and
source metadata. The trusted assembler tracks which complete responses actually
fit. Only the most recent included research response receives a selectable
research handle. A short label never stands in for a report that was omitted
from the actual SDK request. For Stage 4 free questions, that label includes the
exact minimized question recovered from its live erasable receipt. Synthetic
Stage 0 retains its previous behavior.

The source handle receives priority over optional state/history records within
the existing six-source and serialized byte limits. Other admitted reports remain
protected dependencies even when they have no selectable handle. No report,
contradiction or uncertainty field is selectively truncated to make it fit.

## Durable decisions and authority

The existing intellectual proposal and revision journal retains the agent's
chosen stance, confidence, alternatives, goal status/priority/constraints, reason,
review condition and supporting/conflicting source handles. Source classes and
the complete material context binding remain attached to the decision. A revised
record reaches a later actual SDK request after restart. An explicitly retained
position can record an assessment without changing its fields. No-change remains
a valid outcome.

A revision of an existing item may cite counterevidence alone. It retains the
protected prior revision and does not need fabricated positive support. Creating
a new item still requires a supporting source, and one source cannot occupy both
roles in the same decision. Revised inference instructions carry prompt version
`agent-intellectual-state-prompt-v2` in private/public provenance and usage records.

The report class means generated hosted-research model assessment. Citations,
recorded URLs and structural metadata checks are not independent verification.
Every intellectual record remains `unverified`. The prompt asks the model to
weigh limitations and counterevidence; application code does not declare the
model's reasoning correct or invent confidence, opinions, questions or goals.

Private report use inherits the exact private/shared/generic/narrative source
union and live research authorization. Later released expressions and derived
fiction retain their creator's protected research ancestry. Fiction is checked
before ranking, and historical replies are checked before reuse or display.
Withdrawing the research profile hides the report and its dependent
decisions/expressions from future inference even outside the raw history window.
Deletion follows the existing source graph, including quarantined old backups.
Existing generic/typed-memory auxiliary calls do not consume the ordinary reply's
research transcript; their independent source graph remains independent.

Only exact, already-released hosted public reports are omitted from the local
comparison set of private prose during a later research turn. Their raw private
sources, participant inputs and ordinary agent responses remain screened. This
preserves the pre-continuity comparison boundary while avoiding a public report
being mistaken for a private quotation on the next authorized question.

If the complete durable context receipt cannot fit the existing provenance or
byte limits, optional memories, episodes and earlier agent expressions are
omitted together. The participant inputs still form an ordinary bounded request.
A report cannot silently become material context without its authority receipt.
The private H screen is unchanged: its conservative wording/name/overlap checks
can reject harmless public output. This stage does not claim semantic anonymity.

## Validation boundaries

`tests/test_research_intellectual_continuity.py` exercises real HTTP and local
authenticated AF_UNIX boundaries with fake inference/search SDKs. It checks
belief/goal revisions and explicit retention, complete counterevidence in actual
SDK inputs, restart, separate principals, no extra research contact or public
POST, current profile withdrawal, independent memory/fiction boundaries,
erasure/old backups and bounded whole-context omission. These are deterministic integration checks, not evidence
of real-model epistemic judgment, two-human acceptance or production operation.

This branch depends on Stage 4 (PR91). Its ready PR targets `main` cumulatively
because the trusted secret-history scanner accepts only a `main` base. Review
the incremental Stage 5 commit separately; merge Stage 4 before Stage 5 and
refresh current-HEAD CI/review if rebasing changes the latter's HEAD. Shared
cross-surface integration and real-model acceptance remain later planned stages.
