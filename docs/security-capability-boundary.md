# Security capability boundary

This document describes the Stage 0 application boundary between untrusted AI
reasoning and trusted operational authority. It defines interfaces and local
simulation behavior only. It does not claim that provider adapters, public
dispatch, durable policy auditing, restricted network egress, or runtime
isolation have been deployed.

## Trust domains

### A. Untrusted AI data

Conversation text, public text, retrieved memories, mutable self-state,
research results, model output, proposals, and model explanations are data.
They cannot authenticate a principal, change policy, enable a capability,
approve deployment, suspend or restore spending, or alter the emergency stop.
An owner or administrator claim inside any of these values has no operational
authority.

Heuristic screening remains defense in depth. In particular, an H-class
classifier returning `ALLOW` does not cause an action to be authorized. The
deterministic proposal parser and policy checks still apply.

### B. Trusted application control

`TrustedActionContext` contains routing and correlation information supplied by
future authenticated application ingress. `CapabilityState` contains the
application-owned version, emergency-stop state, confirmed provider-spend
suspension state, draft capabilities, and simulation-only state. The model has
no field through which to supply or change either object.

Constructing a trusted context in Python is not authentication. A future
ingress must create it only after authenticating the source. Conversational
`is_owner` text and model-provided participant identifiers are never converted
into this trusted context. Owner status in `TrustedPrincipalContext` also does
not broaden conversational retrieval.

### C. Credential-holding adapters

No credential-holding adapter is implemented in this change. Future OpenAI,
Threads, and other provider adapters must expose narrow application-owned
requests and bounded responses rather than generic URL, header, function, or
tool execution.

Keeping application credentials out of model input is an application data-flow
guarantee implemented here. It is not an operating-system isolation guarantee.
Arbitrary Python code in the same process could read process environment or
mounted files according to that process's OS permissions. Preventing that
requires separately deployed process or container boundaries.

## Secretless model-facing contract

`build_model_request` accepts only `PrivateConversationContext`, verifies the
approved Core Self and context accounting, and constructs `ModelRequest`
field-by-field. The serializer also writes an explicit allowlist; it never uses
`dataclasses.asdict`, `vars`, `__dict__`, database rows, exceptions, environment
variables, or complete trusted contexts.

The model-visible fields are limited to:

- contract version and a closed task kind;
- approved Core Self version, identity, and principles;
- authorized conversation segments labeled as untrusted data;
- whether older authorized history was omitted; and
- the four high-level decisions admitted by this slice.

Serialization is deterministic UTF-8 JSON and has a final byte bound in
addition to the existing context byte and event-count bounds. Authenticated
source references, participant routing identifiers, owner flags, protected
provenance, operational state, adapter configuration, secret paths, and
credentials have no model-facing fields.

Participant content is still ordinary untrusted content. If a participant
chooses to type a credential-like value into an authorized conversation, that
text may be processed under the applicable privacy and provider policies. The
guarantee above concerns application-owned credentials and protected
operational metadata; it is not a claim that arbitrary participant-supplied
secrets can never occur in conversation text.

## Closed proposal boundary

The parser recognizes the complete v0.7 decision vocabulary. It fully validates
only this initial subset:

| Decision | Exact payload | Current behavior |
|---|---|---|
| `no_action` | `decision`, closed `reason_category` | Successful internal decision; never dispatchable. |
| `reply` | `decision`, bounded `content` | Internal private-reply draft simulation only. |
| `research` | `decision`, bounded `brief` | Recognized but unavailable; no research call. |
| `propose_publication` | `decision`, bounded `draft` | Internal publication proposal simulation only. |

`remember`, `revise_belief`, `revise_goal`, `revise_mission`, `defer`, and
`terminate_conversation` are recognized and explicitly reported as not yet
implemented. They are not silently treated as successful actions.

Unknown decisions, missing or extra fields, invalid types, invalid UTF-8, and
oversized text fail closed. This excludes model-supplied recipients, roles,
credentials, approval flags, policy overrides, arbitrary URLs, paths, command
arguments, tool names, and dynamic function names. Natural-language proposal
content remains data and is never evaluated as code or authorization.

## Policy and zero-effect behavior

`ActionPolicy` receives a parse result, trusted routing context, and explicit
capability state. It returns one of these operational outcomes:

- `accepted_internal`;
- `rejected_by_policy`;
- `skipped_stop`;
- `skipped_spend`;
- `unavailable_capability`; or
- `invalid_proposal`.

These outcomes are distinct from the agent decision vocabulary. In particular,
`no_action` is successful deliberate inactivity rather than a failure or skip.

Private reply routing is bound to the trusted participant context, not proposal
content. A private reply can only become an in-memory simulation grant. A
publication proposal can only become an internal draft grant; it does not
broaden private provenance or imply consent, operator approval, outbox state, or
Threads permission. Research remains unavailable until a minimized safe brief,
budget controls, and a dedicated adapter exist.

There is deliberately no execution implementation in this boundary: no shell,
subprocess, filesystem tool, Docker control, HTTP fetcher, provider SDK, dynamic
import, plugin executor, or model-name-to-function mapping. Setting
`simulation_only` to false is rejected because real effects are not implemented.
Consequently, rejected and internally accepted proposals both produce zero real
external effects.

## Revalidation

An accepted simulation grant is bound in memory to the exact validated proposal
object and trusted participant route. `revalidate` checks the current trusted
context and current capability-state version again. A newly active emergency
stop, disabled draft capability, route substitution, grant substitution, or
attempt to leave simulation mode blocks the operation before the fake test sink
is reached.

Provider-spend suspension is checked for research, the paid operation currently
represented at this boundary. It is distinct from a capability denial. No
claim is made that stop activation can revoke an external request that was
already accepted or is in flight; no such external request exists in this
slice.

## Sanitized evidence

`PolicyEvidence.to_audit_record` emits only bounded structured fields:
correlation identifier, decision type, policy version, operational outcome,
closed reason code, capability category, and operational-state version. It
does not serialize proposal text, model prompts, credentials, authentication
references, protected provenance, or participant identifiers.

This evidence is currently in memory. It is suitable input for a future audit
ledger but is not durable audit evidence by itself.

## Privacy remains a separate boundary

Withholding credentials from the model does not make private participant data
safe for sharing or publication. EventStore retrieval and
`PrivateConversationContext` continue to enforce participant scope before model
assembly. Future research and publication still require the contract's
minimization, `abstract_to_shared`, provenance, consent, output screening,
approval, outbox, reconciliation, and dispatch controls. A model proposal can
never stand in for any of them.

## Future runtime isolation

Production deployment must add a separately reviewed physical boundary:

- an AI planning component with no mounted application credential files;
- credential-holding adapters that receive only their own required secrets;
- separate OS or container identities where applicable;
- read-only filesystems and minimal writable mounts;
- no Docker socket and no arbitrary command interface;
- minimal adapter-specific network access;
- narrow application-owned request and bounded response schemas;
- no generic destination, header, or credential parameters; and
- a separate protected owner control plane.

The current Compose services were inspected but not changed. This document does
not claim that the future isolation, network restrictions, adapters, or control
plane already exist.
