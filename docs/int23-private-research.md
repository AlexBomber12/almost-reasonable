# INT-23 private conversation to bounded public research

INT-23 is opt-in. A participant first submits an input through authenticated WSGI
while inference is unavailable. This stores the exact event without contacting a
provider. An independent local operator then reviews a proposed **public**
question against that exact input and any relevant protected context. The operator
must reject a question that reveals a private fact, identity, source identifier,
URL, credential, or recognizable excerpt. A model proposal and the brief regex
screen are inputs to this decision, never proof of public status.

## Local provisioning

Use two separate owner-only files in directories with no group or world write
permission. Every directory on each path must be owned by root or the
corresponding service UID; a shared writable ancestor must have the sticky bit.
The application file stays application-side. The worker file contains
only the public projection. The files have the exact fields shown below; no HTTP
route creates them. An operator creates them locally after reviewing the existing
authenticated event and exact public question. The two files share a newly
generated 32-byte random hex handle, operation ID, question, application UID,
issue time, and expiry. Keep the expiry at most ten minutes after issuance.

Application file (`0600`):

```json
{
  "version": "int23-operator-approval-v1",
  "role": "application",
  "purpose": "private-conversation-public-research-v1",
  "configuration": "public-research-v1",
  "execution_allowance": 1,
  "operation_id": "reply:<sha256 of exact event ID>",
  "question": "How do deep ocean currents form?",
  "authorization": "<64 lowercase random hex characters>",
  "peer_uid": 1001,
  "issued_at": "2030-01-01T00:00:00+00:00",
  "expires_at": "2030-01-01T00:05:00+00:00",
  "reviewer": "<independent operator identity>",
  "reviewed_at": "2030-01-01T00:00:00+00:00",
  "principal_id": "<exact authenticated participant ID>",
  "authenticated_source_ref": "<exact original session source reference>",
  "event_id": "<exact existing input event ID>",
  "input_sha256": "<sha256 of exact UTF-8 input bytes>"
}
```

Worker file (`0600`) contains the same fields from `version` through
`reviewed_at`, with `role` set to `worker`; it has **no** `principal_id`,
`authenticated_source_ref`, `event_id`, or `input_sha256`. The examples are
placeholders, not usable approval or participant data. Compute the operation ID
with `WorkerConversationRuntime._operation_id(event_id)` and the input digest with
`hashlib.sha256(content.encode('utf-8')).hexdigest()`. The operator must compare
the selected event's stored authenticated source, participant, and exact input
before creating either file. `PublicResearchBridge.install_approval` repeats
those checks under the privacy gate. `load_operator_approval` rejects extra or
duplicate fields, unsafe files, invalid intervals and malformed public requests.

Start the worker explicitly with `python -m app.trusted.public_research_worker`
and `--socket`, `--provider-database`, `--approval`, `--stop-file`, `--project-id`,
and `--organization-id`. A separate worker account may use
`--socket-group-gid` for a trusted application/worker IPC group. The stop file
must be a protected `0400` file in a worker-owned `0700` directory, containing
exactly `{"version":"public-research-stop-v1","stop":false}` to allow work;
its ancestors must meet the trusted-directory rule above. Removing or changing
the stop file stops new attempts. The protected provider credential
file is required by the production constructor. Provision the worker's provider
database as a worker-owned `0600` regular file in a worker-owned `0700`
directory before starting the worker. It must already contain the current
EventStore schema: read-only startup admission rejects empty placeholders as
well as missing, linked or insecure databases/ancestors. For the ordinary pilot,
[B05D](b05d-research-deployment.md) reuses the existing worker provider database
without initializing another store. A restart cannot silently replace one-use
reservations.
Provider database ownership stays worker-side. This procedure must not be run
during offline tests.

Start private WSGI with its existing worker options plus
`--enable-public-research --public-research-socket <path>
--public-research-worker-uid <uid> --public-research-approval <application-file>`.
The application IPC state database is separate from the worker provider database.
The client checks the socket location and expected worker UID before sending
request bytes. Remove **both** approval files to revoke the operation across
the two independent processes; each side rechecks its own file at its gates.
Reinstating a file cannot restore an already consumed operation. To approve another input, issue a different exact operation
with a fresh independently reviewed question and handle.

## Boundaries and failure meaning

The application stores research intent and started markers before IPC. The
worker separately reserves its one-use operation before calling the provider.
There is no atomic transaction across these stores. A lost response or crash
after the started marker leaves an uncertain outcome and cannot trigger another
search. A private input binding is never sent to the worker. The only request
fields crossing are operation ID, public question and opaque authorization.
Both the application and worker recheck current authority, stop and spend
controls. The application also checks session and privacy dependencies before
contact, result storage, and reply release. Result prose remains in the reply
and erasure graph. Source records show structural citation metadata only;
`claim_status=unverified` and `semantic_support=not_evaluated` are retained.

Offline connected evidence: `tests/test_int23_connected.py::test_authenticated_wsgi_two_workers_and_durable_reply`
runs authenticated WSGI, a separate inference worker, a separate research
worker, a fake SDK, a cited Russian reply, history, duplicate replay and
approval-file revocation. The fake text establishes no factual ocean-current
claim. No real credentials, provider call, participant enrollment or deployment
is performed. Stage 0 release and real-participant use remain **BLOCKED** pending
the existing privacy, provider and operational approvals.

Proposed scenario bindings, without editing the central acceptance manifest:

| Scenario | Exact test ID |
| --- | --- |
| `SCN-04` connected public research | `tests/test_int23_connected.py::test_authenticated_wsgi_two_workers_and_durable_reply` |
| `SCN-08` stop and spend control | `tests/test_int23_controls.py::test_stop_and_spend_block_contact` |
| `SCN-12` private content exclusion | `tests/test_int23_controls.py::test_unapproved_question_never_crosses_boundary` |
