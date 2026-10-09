# Trusted OpenAI gateway (Stage 0)

This slice adds a disabled-by-default OpenAI Responses API boundary. It does not
activate paid inference, hosted search, publication, or any production service.
Ordinary imports, application startup, database construction, and tests do not
construct an OpenAI client or contact the network. An `OPENAI_API_KEY` environment
variable alone changes nothing.

## Trust boundary

The planner produces only the existing secretless `ModelRequest`. The trusted
request builder combines that validated value with an application-owned model
configuration and versioned prompt. Its closed Responses request sets
`store=false`, `background=false`, `stream=false`, `tools=[]`, bounded
`max_output_tokens`, and disabled automatic truncation. It does not accept a URL,
headers, tools, `previous_response_id`, conversation continuation, retry settings,
or model selection from participant/model data.

`app/trusted/openai_transport.py` is the only credential-holding module. Its
explicit `from_environment()` factory reads `OPENAI_API_KEY`, imports the official
SDK lazily, disables SDK retries, and pins `https://api.openai.com/v1` rather than
honoring an environment-supplied base URL. The gateway owns the single bounded retry
policy. Credentials are absent from domain dataclasses, request bodies, results,
telemetry, exception text, and representations.

This module split is a code trust boundary, not OS isolation. A later deployment
must run planner and credential holder under separate identities; mount no provider
credential into the planner; give the worker only its required key; expose no
public credential-bearing endpoint; strongly authenticate and narrowly scope
worker requests; restrict egress; use read-only/minimal mounts; provide no Docker
socket, generic shell, filesystem executor, or unrestricted tool framework; and
return bounded secretless responses. Those runtime protections are not active yet.

## Provider contract verified from current documentation

Checked on 2026-10-10:

- The [Responses create reference](https://developers.openai.com/api/reference/resources/responses/methods/create)
  documents `store`, `background`, `max_output_tokens`, `tools`, `text`, and
  `previous_response_id`. The pinned official Python SDK 3.28.0 exposes those
  parameters and a per-call timeout.
- The [Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=responses)
  documents strict JSON Schema under `text.format`, requires object fields to be
  required and objects to use `additionalProperties: false`, and recommends output
  schemas rather than function tools for structured text.
- The [model catalog](https://developers.openai.com/api/docs/models) currently
  identifies `gpt-6-luna` and `gpt-6.1-sol` as Responses-capable models with
  Structured Outputs. They are the only accepted identifiers. There is no fallback
  or automatic escalation between them.
- The [error guide](https://developers.openai.com/api/docs/guides/error-codes)
  documents `project_spend_limit_exceeded` and
  `organization_spend_limit_exceeded` separately from `rate_limit_error`,
  `slow_down`, credit exhaustion, usage limits, authentication, invalid requests,
  permissions, connection failures, timeouts, and server/overload errors.

Account-specific model access, real response serialization, actual usage fields,
provider error bodies/headers, timeout behavior, and project-limit behavior still
require a separately authorized Stage 1 live smoke test. Documentation does not
prove availability for this project. No live request was made for this PR.

## Output and policy handling

The provider is asked for one strict root object covering only `no_action`,
`reply`, `research`, and `propose_publication`. Every field remains required by the
provider schema; unused fields must be null. Locally, the gateway checks response
status and output item types, refuses tool/function output, handles refusal and
incomplete responses separately, bounds UTF-8 output, parses JSON, rejects unknown
or mismatched fields, and finally calls the existing `parse_agent_proposal`.

Provider schema success is not authorization. The returned `ProposalValidation`
is an untrusted proposal for the existing `ActionPolicy`; the gateway executes no
reply, research, publication, or other effect. `research` remains a proposal and
never enables hosted `web_search`.

## Retries, stop, and failure behavior

Configuration bounds every attempt timeout, overall allowance, output size, number
of attempts (maximum three), exponential backoff, and injected jitter. The gateway
rechecks the trusted emergency stop and durable spend suspension before every paid
attempt. A request already transmitted may still complete; the stop cannot revoke
it.

Only ordinary documented rate limits, transport timeouts/connections, and 5xx
server failures retry. Confirmed spend limits, other billing/quota conditions,
authentication, invalid requests, permissions, malformed/refused/incomplete
responses, and ambiguous 429 errors do not. A bare 429 is never called confirmed
spend exhaustion. Operational failures carry fixed Russian user-safe text and do
not trigger another model call.

## Schema v3, suspension, and telemetry

SQLite schema v3 adds `provider_spend_suspensions`,
`provider_attempt_reservations`, and `provider_usage_attempts`. Fresh initialization
and the transactional v1/v2-to-v3 migrations remain WAL-backed, serialized by
`BEGIN IMMEDIATE`, idempotent, and fail closed on unsupported versions.

A structured confirmed project or organization spend-limit error writes the usage
attempt and durable suspension in one transaction before the gateway reports the
suspension as established. Restart, month changes, participant text, owner chat,
and model output cannot clear it. There is intentionally no reset API. A future
authenticated owner control plane must verify provider state and explicitly record
reenablement. Persistence failure latches the running gateway closed.

Telemetry contains bounded identifiers, provider/model/config/prompt/Core Self
versions, provider response ID when available, timestamp/duration, documented token
counts when supplied, outcome/error classification, retry count, and an optional
estimate. It contains no prompt, response body, credential, authorization header,
or raw provider message. Missing usage or matching versioned pricing is represented
as unavailable/incomplete, never zero.

Cost uses exact `Decimal` arithmetic and only explicit operator-supplied per-million
token prices. It is informational, not a reservation ledger or invoice. The
provider's USD 10 monthly project hard limit is the primary monetary boundary, but
provider timing, token accounting, delayed reporting, taxes, credits, and an
in-flight/lost response mean it is not a mathematical invoice guarantee.

Attempt IDs are bounded. Before provider contact, SQLite atomically checks the
durable spend suspension and reserves the attempt ID; concurrent duplicates cannot
both reach the provider. Final telemetry replaces that reservation in one
transaction. SQLite still cannot atomically commit with an external provider call.
A crash or response lost after provider acceptance leaves the durable reservation,
may have incurred usage without local token telemetry, and fails closed rather than
automatically replaying the same operation.

## Future Stage 1 smoke validation

After separate operator authorization, use a dedicated project with the verified
hard limit and synthetic non-sensitive input. Supply the credential only to the
isolated worker, explicitly select an allowlisted model and Stage 1 configuration,
make one tightly bounded request, and inspect sanitized request/response, usage,
error, and suspension evidence. Confirm `store=false`, no tools/continuation, SDK
and model compatibility, response item shapes, token counts, structured error
codes, and the provider console's spend state. Do not use private participant data,
publish anything, or treat smoke authorization as production activation.
