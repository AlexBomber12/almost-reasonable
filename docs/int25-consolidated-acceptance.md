# INT-25 consolidated offline acceptance

The sole scenario status and exact node-ID inventory is
[acceptance/scenarios.json](../acceptance/scenarios.json). INT-25 adds evidence to
SCN-01, 03, 04, 06, 09, 10, 11 and 12 without changing any complete-MVP status.
The mappings distinguish component checks, connected integration and recovery
checks. `python -m acceptance.run` checks every mapping against actual pytest
collection and records setup, call and teardown outcomes from the same gate run.

## Connected path

`tests/test_int25_connected.py` uses authenticated private WSGI input, an exact
INT-23 application approval, separate guarded inference and public-research child
workers, and the merged durable private runtime. After a completed research reply,
it reopens that runtime and repeats the operation: the durable response is returned
and the consumed inference and research authorities are not replayed. The test then
runs the independent INT-24 public worker, observes an approval-bound outbox draft,
applies local approval and dispatches through fake Threads HTTP. A private canary
must be absent from the public research request, public worker SDK input and fake
Threads request. The same application OwnerStore Stop prevents new private and
public work. INT-25 child workers install application offline guards themselves.
The mapped INT-23 test uses an acceptance wrapper to install those guards in its
spawned interpreters. A separate child-process regression proves Internet sockets,
DNS, live HTTP and default credential construction are denied there.

Existing mapped recovery checks additionally cover INT-22 allowance persistence,
INT-23 lost research responses, and INT-24 started inference, unknown publication,
interrupted sending, one-use worker authority and accounting uncertainty. These
checks use fake SDK or HTTP responses and local stores. The guard rejects Internet
sockets, DNS, live HTTP and default credential/provider construction in mapped
processes. It is not an OS network sandbox.

## Readiness boundary

INT-22's private pilot, INT-23's approved public research, and INT-24's public
Threads runtime remain explicit offline configurations. The public research result
is labeled unverified; no semantic source-support check is established. Synthetic
memory extraction does not establish live-model memory behavior. The recorded
narrative policy and eight qualitative human-pilot judgments retain their original
meaning: six `acceptable`, two `specific_error_reported`, with no complete H/M
calibration, numerical score or inferred agreement with owner opinion. Human
behavioral evaluation, live provider and Meta validation, enrollment, and
operational deployment/recovery evidence remain outstanding. Deterministic gate
success therefore still yields release readiness `BLOCKED` and runner exit 2.
