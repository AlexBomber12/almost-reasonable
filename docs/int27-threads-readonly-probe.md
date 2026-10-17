# INT-27 Threads read contract probe

This operator tool is disabled by default. `python -m app.ops.int27_threads_readonly_probe` prints `{"state":"disabled","requests":0}` without reading a configuration, token, owner store, or network. The separate `run` command is reserved for a later, explicitly authorized operator verification. This task did **not** run it against Meta.

## Official documentation checked on 2026-10-17

Meta's [Threads profile request](https://www.postman.com/meta/threads/request/34203612-3996f0a2-f2ee-4af7-9b66-3dbbe80b8d7d) shows `GET /me?fields=...`; the probe requests only `id` and compares it with the reviewed provider user ID. Meta's [Threads replies request](https://www.postman.com/meta/threads/request/34203612-11c959db-21cb-4303-b717-73b661b3579c) shows `GET /{thread_id}/replies` with `fields` and `reverse=false`. The existing transport adds `limit=10` and asks for `id,text,media_type,timestamp,root_post,replied_to,is_reply_owned_by_me`, matching the fields in Meta's reply example.

Meta's [mentioned posts request](https://www.postman.com/meta/threads/request/34203612-fc3f21da-0a53-44ab-80e2-8cd8c376a42a) shows `GET /me/mentions?fields=...&limit=50`. The pinned transport instead uses `GET /{provider_user_id}/mentions` with `limit=10`. The official example does not establish that the ID route is interchangeable with `/me`; treat that route and access to it as unresolved until Meta documentation or an explicitly authorized live observation verifies it. Meta's [collection overview](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api) says its examples may lag the developer documentation. Direct developer pages returned HTTP 429 during this check. None of these sources proves this app has App Review approval or a specific permission grant.

## Later operator procedure; requires separate authorization

1. Confirm the repository revision, reviewed Threads account reference and provider user ID, reply thread ID, configured streams, existing owner Stop directory, and the needed read permissions. Resolve the mentioned-posts route discrepancy before selecting `mention`. Confirm the owner Stop is inactive. Do not clear Stop for the probe.
2. Outside the repository, prepare a private directory owned by the invoking user, mode `0700`, under a trusted directory chain. Put the existing Threads token in a single-link regular file owned by that user, mode `0400`. Do not put a token in a shell argument, environment variable, repository, or receipt. The tool does not create an authorization file.
3. Create a separate, manually reviewed single-link regular JSON configuration file in a private directory, mode `0400`, with exactly these fields. `expires_at` must be an aware ISO 8601 time in the future and no more than one hour away. Set `revision` to the exact local `git rev-parse HEAD` value. Select one or both streams in request order, with no duplicates. The example values are placeholders, not a usable authorization:

   ```json
   {
     "version": "int27-threads-readonly-probe-v1",
     "purpose": "threads-read-contract",
     "reviewed": true,
     "expires_at": "REPLACE_WITH_NEAR_FUTURE_AWARE_TIME",
     "revision": "REPLACE_WITH_REVIEWED_GIT_HEAD",
     "account_ref": "REPLACE_WITH_REVIEWED_ACCOUNT_REFERENCE",
     "provider_user_id": "REPLACE_WITH_REVIEWED_PROVIDER_USER_ID",
     "reply_thread_id": "REPLACE_WITH_REVIEWED_REPLY_THREAD_ID",
     "streams": ["reply"],
     "owner_directory": "/REPLACE/WITH/EXISTING/OWNER/STORE/DIRECTORY",
     "token_file": "/REPLACE/WITH/PROTECTED/TOKEN/FILE"
   }
   ```

4. Only after separate live authorization, invoke `python -m app.ops.int27_threads_readonly_probe run --config /absolute/protected/config.json`. Save the sanitized JSON receipt in an operator-approved location. A nonzero exit means the observation failed and requires investigation. Never rerun automatically.

The tool uses the existing `ThreadsHTTPTransport` and `HTTPSClient` with TLS verification and redirects disabled. A second boundary allows only the transport's exact `GET` URLs. It checks the existing owner Stop and configuration expiry before each request, stops after account verification on mismatch, and makes at most three requests: one account, at most one replies page, and at most one mentions page. Each page requests at most ten records. Each HTTP read is bounded by the existing 2,097,152-byte response cap. The invocation has a 30-second monotonic deadline, no retry, and no pagination loop. Response links and cursors are never followed. The probe does not construct the public coordinator, model client, scheduler, publication service, outbox, or polling stores.

The receipt contains the repository revision, reviewed account and route binding, start and finish timestamps, request count, classified outcome, observed field names, and page record counts. It omits token material, headers, response bodies, and inbound text. `observed` means only that these bounded requests returned parseable account-bound read shapes. `account_mismatch`, `authentication_failure`, `rate_limited`, `contract_malformed`, `response_oversized`, `deadline_exceeded`, `configuration_expired`, `owner_stop`, and other classified failures stop subsequent reads. A successful receipt does not establish App Review approval, all configured scopes, publishing permission, semantic correctness, or complete Stage 3 readiness. Those remain explicit unresolved receipt entries.
