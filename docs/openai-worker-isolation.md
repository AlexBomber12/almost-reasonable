# Isolated OpenAI worker (Stage 0)

This slice makes the credential-holder boundary executable as a separate local
process. It does not deploy that process, enable a paid model call, add a TCP or
HTTP listener, or change Compose. The supplied executable harness always uses the
fake transport and the inert Stage 0 provider configuration, even when OpenAI
environment variables are present.

## Process and trust architecture

The v1 application client accepts an already authorized secretless `ModelRequest`. INT-19 adds an explicit private-web runtime with typed memory/shared context over `openai-worker-context-v2`; see [private-worker-conversation.md](private-worker-conversation.md).
`OpenAIWorkerClient` sends that value plus application-generated operation and
correlation identifiers over an AF_UNIX socket. It neither imports the OpenAI
transport nor constructs an SDK client, and it has no parameter for credentials,
headers, provider URLs, model selection, tools, deployment stage, retries, spend
resets, approvals, paths, commands, or queries.

The worker authenticates the local process, decodes the request through exact
allowlists, reconstructs typed application values, and reruns all existing
`ModelRequest` validation. It then calls the existing `TrustedOpenAIGateway`, which
owns provider configuration, retry/deadline checks, emergency-stop checks,
durable attempt reservation, spend suspension, telemetry, normalization, and the
credential-capable transport boundary. The default harness injects
`FakeOpenAITransport`; it never calls `OpenAIResponsesTransport.from_environment`.

The worker returns a validated but still untrusted proposal or a sanitized
operational outcome. It never executes the proposal. The application must still
send any proposal through `ActionPolicy`; `reply`, `research`, and
`propose_publication` remain drafts or unavailable capabilities, and `no_action`
remains legitimate inactivity.

Privacy authorization remains an application-ingress responsibility. Peer
authentication proves which local process connected; it does not authenticate an
end-user by inspecting conversation text. The worker never opens the participant
conversation database.

## Protocol and bounds

Protocol `openai-worker-v1` has one operation, `infer`. A request contains exactly
`protocol_version`, `operation_type`, `operation_id`, `correlation_id`, and the
existing allowlisted `model_request`. A response contains exactly
`protocol_version`, `outcome`, `attempts`, an optional bounded safe Russian
operational message, and an optional validated `agent_proposal`. Provider response
IDs, raw provider bodies, protected principals, authenticated source references,
database rows, exceptions, SDK objects, and credentials never cross this boundary.

Each message is one four-byte network-order length followed by UTF-8 JSON and EOF
on the sending half of the connection. Pickle, dynamic imports, and object
construction from type names are not used. The implementation enforces:

- a 1,146,880-byte maximum frame (the existing model-request bound plus 64 KiB of
  protocol/JSON overhead), checked before body allocation;
- a maximum JSON nesting depth of 16;
- a 320-second default connection lifetime, bounded from 320 to 340 seconds from
  the existing 300-second provider overall-timeout ceiling plus IPC overhead, and
  a maximum 10-second individual socket-I/O timeout;
- exact object fields and strict string, integer, boolean, enum, and identifier
  types;
- duplicate-field, malformed JSON, malformed UTF-8, empty, truncated, oversized,
  trailing-data, and unknown-version rejection; and
- the existing context, request, proposal, provider-output, and attempt limits.

The default client uses a 320-second lifetime and five-second I/O timeout. Request
transfer remains subject to the I/O timeout, while the first response byte may
wait within the total lifetime so the existing bounded provider retry/deadline
sequence can finish. It makes one exchange and never retries. Failure before
connection is `communication_failure`. Any failure after transmission begins is
conservatively `outcome_uncertain`, with the original operation and correlation
IDs preserved.
The caller must reconcile that identity; it must not invent a new operation ID.
The durable gateway reservation prevents the same operation identity from being
executed twice, but no SQLite transaction can prove whether a provider call
completed after a worker crash or lost response.

The executable serve loop permits exactly one active gateway operation. It keeps
accepting while that operation is in flight and immediately closes excess local
connections, so a request cannot wait in the listen backlog and later trigger a
paid call after its client lifetime expired. A client that already began
transmission conservatively reports `outcome_uncertain`; the rejected request is
not processed or queued.

After decoding, the worker verifies that the IPC lifetime still contains the
existing maximum gateway overall timeout plus response-I/O allowance. If a peer
consumed that reserve while slowly framing its request, the worker returns a
sanitized timeout without invoking the gateway or provider.

## Peer and filesystem authentication

On Linux, the server requires `SO_PEERCRED` and compares the kernel-reported UID
with a nonempty trusted worker configuration allowlist before reading a request.
No UID, username, owner flag, principal, or bearer proof in JSON has authority. If
the peer facility is unavailable or unreadable, the worker fails closed.

The socket path must be absolute, absent, and at most 107 UTF-8 bytes. Its immediate
directory must already exist, resolve without symbolic links, be owned by the
worker effective UID, and have no group/world write bits. Private mode remains the
default: startup uses a restrictive umask and verifies a worker-owned `0600`
socket. Restricted group mode is enabled only by an explicit trusted IPC GID. It
requires a worker-owned `0710` parent in that GID, changes the new socket to that
GID, and verifies the same inode is worker-owned, in the configured GID, and
exactly `0660`. World socket access is never accepted. It never deletes a
pre-existing file, socket, or symlink. Shutdown records the created socket's
device/inode and removes only that exact still-present socket, so path substitution
cannot cause deletion of another resource.

Create a dedicated directory owned by the worker before startup. After an
unclean crash, an operator must verify that no worker is running, inspect the exact
path with `lstat`, remove only the confirmed stale socket, and restart. The worker
does not silently perform stale-socket recovery.

Mode `0660` grants filesystem access only through the dedicated IPC group; it does
not authorize a request. Every connection is still checked against the nonempty
kernel UID allowlist before any frame is read. Group membership alone, a UID in
JSON, or an unexpected user-namespace mapping has no authority. The optional
Stage 0 peer-UID diagnostic logs only the kernel UID and authorization result so an
operator can verify the effective mapping.

## Persistence and credentials

The worker opens a dedicated SQLite file through the existing `EventStore` schema
v3 and `ProviderStateStore`. Schema reuse creates currently unused application
tables; it does not copy participant conversations into the worker database. The
planner needs neither read nor write access to this file. Spend suspension,
atomic attempt reservation, duplicate-operation protection, sanitized usage
telemetry, retry bounds, and persistence-failure latching remain gateway-owned and
survive worker restart or IPC failure.

Only the trusted worker process may be given OpenAI credentials and explicitly
construct `OpenAIResponsesTransport` in a future separately approved Stage 1
harness. Credential loading is never performed by import, application startup, or
the offline harness. Synthetic canary tests verify that client frames, worker
responses, representations, and errors do not contain application credential
values.

## Running the offline harness

For the same-UID development harness, prepare a private directory and run the
worker and client as separate processes. The directory and paths below are
examples; do not place real provider secrets in the environment or database:

```console
install -d -m 0700 /tmp/almost-reasonable-worker
python -m app.trusted.openai_worker_server \
  --socket /tmp/almost-reasonable-worker/openai.sock \
  --database /tmp/almost-reasonable-worker/provider.sqlite3 \
  --allow-uid "$(id -u)"
```

The application constructs `OpenAIWorkerClient` with that socket path and calls
`infer` with an already validated `ModelRequest` and stable operation/correlation
IDs. `--max-connections N` is available for deterministic offline tests. There is
no option that enables Stage 1, paid calls, a real transport, tools, or public
effects. `scripts/ci.sh` exercises real loopback Unix sockets and a worker in a
separate OS process without Docker or root privileges.

The opt-in cross-UID container topology is defined separately in
`ops/compose.openai-worker.stage0.yaml`. It uses worker UID/GID `11001:11001`,
planner UID/GID `11002:11002`, and IPC GID `11000` only as overridable examples.
Both services use `network_mode: none`, read-only roots, no capabilities,
no-new-privileges, bounded memory/PIDs, and no published ports or Docker socket.
Only the worker mounts its database directory; the probe receives only a read-only
bind of the socket directory. See `docs/openai-worker-stage0-deployment.md` for
safe host preparation and the mandatory rootless/userns checks.

## Remaining deployment work and Stage 1

Process separation was implemented before this deployment preparation. This PR
adds configurable distinct container identities and testable filesystem policy,
but actual kernel enforcement still depends on the operator's Docker, bind-mount,
rootless, and user-namespace configuration. The offline topology demonstrates the
absence of a planner credential mount; it cannot demonstrate secrecy of a real
credential because none is present. Provider networking is intentionally absent.
Before a live Stage 1 smoke test, a separately reviewed deployment must:

- run planner and worker under distinct, least-privileged Linux/container users;
- give only the worker a dedicated secret mount and provider-database write mount;
- give the planner no credential, provider database, or worker filesystem access;
- provide a reviewed group/ACL socket design and verify `SO_PEERCRED` authorization;
- use read-only/minimal filesystems and mounts, with no Docker socket or shell;
- restrict network egress so only the worker can reach the pinned OpenAI endpoint;
- wire an authenticated operational stop source outside conversational data; and
- validate account/model access, `store=false`, response shapes, timeouts, usage,
  spend-limit behavior, redaction, process inspection resistance, and mount/egress
  policy using synthetic non-sensitive input.

That work requires new operator authorization. This repository change makes no
real API request, production service change, deployment, or claim that those
physical controls already exist.
