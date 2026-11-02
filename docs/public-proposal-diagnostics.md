# Public proposal rejection diagnostics

The deployed public model sometimes recorded `provider_error` after receiving
usage, while the owner view displayed an unknown outcome. Those old records do
not identify the local failure stage. Rejected model text is not retained and
`store=False` remains in effect, so this change cannot reconstruct old replies.

The public SDK adapter now reports one of eight fixed codes for its existing
usage, completion, refusal, output size/type, JSON, action, mission and research
checks. The native worker persists the code through the existing
`provider_error_code` field. The telemetry reader and owner UI accept only this
closed vocabulary. Ordinary provider errors, including legacy rows with no
stage code, display a generic provider failure; arbitrary external codes remain
hidden. No response text, exception string, source data or credential is added
to diagnostic records.

Admission, strict action parsing, one-use attempts, accepted output, SDK
requests, deadlines, research permissions, billing settings and database schemas
are unchanged. Invalid proposals remain rejected; there is no salvage, retry or
replay. A stage diagnosis is an observation, not authorization to weaken a gate
or a claim that the underlying model error has been corrected.

Ship model worker, telemetry reader and owner UI from a coherent reviewed
candidate. Older diagnostic decoders safely decline unfamiliar stage codes.
Current-HEAD full local CI, GitHub CI and Codex Review remain required. Regression
tests use synthetic stores and fake SDKs; real model/human/public acceptance is
separate. Future live observations are still needed to identify the actual
stage of the intermittent public failure.
