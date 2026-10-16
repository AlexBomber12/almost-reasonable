# INT-21: Threads transport contract and offline evidence

Verified 2026-10-16 against Meta's official [Threads API collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api), [text container](https://www.postman.com/meta/threads/request/34203612-74fb48b1-ad1a-480e-b200-4dcdb8126a2f), [publish](https://www.postman.com/meta/threads/request/34203612-c940a17f-e719-4b5b-9d28-7390bc658fb7), [replies](https://www.postman.com/meta/threads/folder/2tx5bse/read-and-manage-threads), and [mentions](https://www.postman.com/meta/threads/request/34203612-fc3f21da-0a53-44ab-80e2-8cd8c376a42a). The collection says it may lag the [Meta changelog](https://developers.facebook.com/docs/threads/changelog). No live account, token, app review or granted scope was inspected.

## Supported offline contract

| Operation | Meta endpoint | Required scope evidence in local policy | Accepted evidence |
| --- | --- | --- | --- |
| Account binding | `GET /me?fields=id` | `threads_basic` | ID equals configured Meta user ID |
| Text container | `POST /{user}/threads` | `threads_basic`, `threads_content_publish` | Returned container `id` |
| Publish container | `POST /{user}/threads_publish` | `threads_basic`, `threads_content_publish` | Returned post `id`, then `GET /{id}` owner/parent check |
| Replies to configured root | `GET /{root}/replies` | `threads_basic`, `threads_read_replies` | Complete parsed page with matching `root_post`, `replied_to`, and cursor |
| Mentions of configured user | `GET /{user}/mentions` | `threads_basic`, `threads_manage_mentions` | Complete parsed page with provider IDs and cursor |

The scopes above are service policy checks, not claims that the Meta app or current token actually has those permissions. The official collection documents the two-step container/publish flow, reply and mention reads, and cursor paging. It does not establish a safe retry rule for a lost write response or a numeric rate quota for this specific account. HTTP 429 therefore remains uncertain after a write; an offline fixture does not establish live rate behavior.

- A trusted, explicit `ThreadsHTTPTransport` binds the local account, Meta user ID, root post ID, token and HTTP client. The destination and version are fixed to `https://graph.threads.net/v1.0`; no caller can supply a URL. OAuth bearer credentials stay in the authorization header. The standard HTTPS client verifies TLS, disables redirects, caps each response at 2 MiB, and returns or times out within the configured total caller deadline of at most 10 seconds. Real inbound requests use at most 10 items per page, leaving room within that response bound for fully escaped text at the 16 KiB per-item input limit. A timed-out write may still finish in its background worker and is classified uncertain; one in-flight request per transport prevents accumulating workers. The service accepts this transport only with an explicit `live_enabled=True` Stage 3 binding. Stage 0 still admits only the exact `ThreadsSimulator`.
- `GET /me?fields=id` checks the authenticated account before each write and before the durable write-dispatch marker. Approval, privacy and stop controls are rechecked after that read and before the marker. A failed or interrupted preflight read is a proven no-write result. `POST /{user}/threads` creates a `TEXT` container, with `reply_to_id` for a reply. `POST /{user}/threads_publish` publishes its `creation_id`. The returned publication ID is stored durably before `GET /{id}` checks the same owner and expected parent. Actual returned container and publication IDs use existing columns, so no schema migration is needed.
- Structured 400, 401 and 403 Graph errors reject an operation. A structured 429, server error, redirect, missing ID, malformed or oversized response, interrupted request, or failed publication verification leaves the operation `UNKNOWN` when a write may have occurred. The account remains paused. Current approval, per-draft privacy, policy, stop, capability and outbox reservations remain at the existing service boundary. An explicit pre-transmission failure may use the existing bounded retry path.
- `GET /{root}/replies` requests `reverse=false` for chronological pages, so a durable `after` cursor can reach later arrivals. Account-owned replies are validated and skipped while the page cursor advances. Valid image, video and carousel items without text are skipped after source and routing checks; the page cursor still advances. `GET /{user}/mentions` has no verified forward-order parameter in the official collection, so a completed bounded scan resets its durable cursor and the next poll starts at the head; interrupted scans continue from their saved `after` cursor. `paging.next` controls continuation; a terminal page may still carry an `after` cursor. Each real page request is capped by the remaining service event budget; the service stops fetching when that budget is full and commits the bounded prefix. The transport validates every event before returning a verified page. The service commits all fetched pages, events and the final cursor in one transaction. A malformed later page commits none of them. Reply routing requires the configured root ID and a provider parent ID. Mention records keep provider IDs.

## Deliberately unresolved

The published contract does not provide an operation key that proves a lost write response did or did not create exactly this publication. If the publish response yielded a real post ID before readback failed, reconciliation can query that exact ID and confirm only when owner and parent bindings match. Otherwise `reconcile` returns `UNRESOLVED`; it never searches by text or time, synthesizes a provider ID, republishes, or authorizes a replacement. Administrative `UNKNOWN_ABANDONED` remains the existing purpose-bound path. A separately reviewed strategy with exact provider evidence is needed to resolve effects whose response ID was lost.

The Meta collection uses unversioned examples; this implementation pins `v1.0` as trusted configuration. Version acceptance and bearer-header behavior require an authorized live smoke before activation. App review, current account scopes, token validity, publishing limits, endpoint field availability, and mention/reply coverage cannot be established from requested scopes or offline responses. Public keyword search, token refresh, profile edits, general model-generated publication, deployment configuration and an unauthenticated publish endpoint are outside this milestone. The fixed root ID limits reply polling to one configured conversation; broader discovery needs a separate reviewed design.

The mention head-rescan strategy avoids treating a reverse-ordered `after` cursor as a permanent checkpoint. Under sustained arrivals faster than the bounded page budget, a scan may take multiple polls to reach its terminal page; operational backlog limits and Meta's actual ordering require live validation before activation.

The existing `app/oauth_server.py` is a state-checked callback that stores an authorization code; it is not a token exchange, token loader, account access verifier, or refresh service. This PR leaves it unchanged. A future live smoke requires a separately authorized credential path and an independent check of the actual Meta app/account permissions.

## Offline test IDs and proposed acceptance mapping

Do not edit `acceptance/scenarios.json` in this parallel PR. Proposed mappings for later integration:

| Test ID | Proposed scenario |
| --- | --- |
| `test_two_stage_post_verifies_real_identity_through_service` | `INT-21-PUBLISH-IDENTITY` |
| `test_reply_parent_is_sent_and_verified` | `INT-21-REPLY-ROUTING` |
| `test_failures_preserve_effect_class_and_never_resend` | `INT-21-UNCERTAIN-RECOVERY` |
| `test_disabled_real_transport_and_missing_current_approval` | `INT-21-DEFAULT-DENY` |
| `test_mentions_poll_persists_cursor_and_pending_event` | `INT-21-MENTION-CURSOR` |
| `test_reply_poll_rejects_wrong_root_without_advancing` | `INT-21-REPLY-CURSOR-SAFETY` |
| `test_http_adapter_bounded_reads_and_redirect_suppression` | `INT-21-HTTP-BOUNDARY` |
| `test_transport_rejects_untrusted_configuration` | `INT-21-TRUSTED-CONFIG` |
| `test_container_response_is_never_accepted_without_valid_id` | `INT-21-MALFORMED-RESPONSE` |
| `test_transport_rejects_foreign_outbound_items` | `INT-21-ACCOUNT-BINDING` |
| `test_malformed_mention_pages_do_not_advance_cursor` | `INT-21-PAGE-ATOMICITY` |
| `test_valid_reply_and_mention_parent_are_preserved` | `INT-21-INBOUND-PARENT` |
| `test_invalid_stored_cursor_and_stream_rejected_before_http` | `INT-21-CURSOR-VALIDATION` |
| `test_account_preflight_prevents_publication_to_wrong_profile` | `INT-21-ACCOUNT-PREFLIGHT` |
| `test_explicit_live_binding_requires_real_transport_and_stage_3` | `INT-21-LIVE-BINDING` |
| `test_later_malformed_page_does_not_commit_earlier_events` | `INT-21-PAGE-ROLLBACK` |
| `test_known_publication_id_reconciles_from_exact_provider_readback` | `INT-21-EXACT-ID-RECONCILIATION` |
| `test_known_id_with_wrong_parent_remains_unresolved` | `INT-21-RECONCILIATION-BINDING` |
| `test_reconciliation_cannot_substitute_a_different_publication_id` | `INT-21-RECONCILIATION-IDENTITY` |
| `test_failed_account_preflight_has_proven_no_write` | `INT-21-PREFLIGHT-CLASSIFICATION` |
| `test_terminal_page_after_cursor_does_not_fetch_again` | `INT-21-TERMINAL-PAGE` |
| `test_publication_id_is_durable_before_readback_interruption` | `INT-21-DURABLE-PUBLICATION-ID` |
| `test_sending_evidence_rejects_invalid_publication_id` | `INT-21-ID-VALIDATION` |
| `test_http_adapter_enforces_total_deadline_and_one_inflight_request` | `INT-21-TOTAL-DEADLINE` |
| `test_http_adapter_worker_error_and_start_failure_are_bounded` | `INT-21-HTTP-WORKER-FAILURE` |
| `test_forward_reply_poll_reaches_new_arrivals_after_saved_cursor` | `INT-21-FORWARD-REPLIES` |
| `test_store_rejects_ambiguous_cursor_reset` | `INT-21-CURSOR-RESET-VALIDATION` |
| `test_crash_during_account_preflight_recovers_without_unknown` | `INT-21-PREFLIGHT-CRASH-RECOVERY` |
| `test_live_pages_stop_at_event_budget_and_resume_without_loss` | `INT-21-PAGE-BUDGET` |
| `test_owned_reply_is_skipped_and_cursor_advances` | `INT-21-OWN-REPLY-SKIP` |
| `test_live_page_limit_is_validated_before_http` | `INT-21-REQUEST-LIMIT` |
| `test_provider_page_exceeding_requested_limit_keeps_cursor` | `INT-21-PAGE-LIMIT-ENFORCEMENT` |
| `test_stop_during_account_preflight_is_rechecked_before_write` | `INT-21-PREFLIGHT-STOP-FENCE` |
| `test_full_page_of_long_valid_text_fits_response_bound_and_advances` | `INT-21-LONG-PAGE-CURSOR` |
| `test_media_only_item_is_skipped_and_cursor_advances` | `INT-21-MEDIA-CURSOR` |
| `test_missing_text_without_supported_media_type_fails_closed` | `INT-21-MISSING-TEXT-VALIDATION` |

All responses in these tests are deterministic fakes; ordinary CI has no Meta token or connection. Release readiness remains `BLOCKED` until a separately authorized live smoke verifies the exact app, account, scopes and endpoint contract.

### Exact collected test IDs

```text
tests/test_int21_threads_transport.py::test_two_stage_post_verifies_real_identity_through_service
tests/test_int21_threads_transport.py::test_reply_parent_is_sent_and_verified
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[container-responses0-failed_terminal]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[container-responses1-failed_terminal]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[container-responses2-unknown]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[container-responses3-unknown]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[container-responses4-unknown]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[container-responses5-unknown]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[container-responses6-unknown]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[publish-responses7-failed_terminal]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[publish-responses8-unknown]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[publish-responses9-unknown]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[verify-responses10-unknown]
tests/test_int21_threads_transport.py::test_failures_preserve_effect_class_and_never_resend[verify-responses11-unknown]
tests/test_int21_threads_transport.py::test_disabled_real_transport_and_missing_current_approval
tests/test_int21_threads_transport.py::test_mentions_poll_persists_cursor_and_pending_event
tests/test_int21_threads_transport.py::test_reply_poll_rejects_wrong_root_without_advancing
tests/test_int21_threads_transport.py::test_http_adapter_bounded_reads_and_redirect_suppression
tests/test_int21_threads_transport.py::test_transport_rejects_untrusted_configuration[changes0]
tests/test_int21_threads_transport.py::test_transport_rejects_untrusted_configuration[changes1]
tests/test_int21_threads_transport.py::test_transport_rejects_untrusted_configuration[changes2]
tests/test_int21_threads_transport.py::test_transport_rejects_untrusted_configuration[changes3]
tests/test_int21_threads_transport.py::test_transport_rejects_untrusted_configuration[changes4]
tests/test_int21_threads_transport.py::test_container_response_is_never_accepted_without_valid_id[bad_response0]
tests/test_int21_threads_transport.py::test_container_response_is_never_accepted_without_valid_id[bad_response1]
tests/test_int21_threads_transport.py::test_container_response_is_never_accepted_without_valid_id[bad_response2]
tests/test_int21_threads_transport.py::test_container_response_is_never_accepted_without_valid_id[bad_response3]
tests/test_int21_threads_transport.py::test_container_response_is_never_accepted_without_valid_id[bad_response4]
tests/test_int21_threads_transport.py::test_container_response_is_never_accepted_without_valid_id[wrong type]
tests/test_int21_threads_transport.py::test_transport_rejects_foreign_outbound_items
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload0]
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload1]
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload2]
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload3]
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload4]
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload5]
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload6]
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload7]
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload8]
tests/test_int21_threads_transport.py::test_valid_reply_and_mention_parent_are_preserved
tests/test_int21_threads_transport.py::test_invalid_stored_cursor_and_stream_rejected_before_http
tests/test_int21_threads_transport.py::test_account_preflight_prevents_publication_to_wrong_profile
tests/test_int21_threads_transport.py::test_explicit_live_binding_requires_real_transport_and_stage_3
tests/test_int21_threads_transport.py::test_later_malformed_page_does_not_commit_earlier_events
tests/test_int21_threads_transport.py::test_known_publication_id_reconciles_from_exact_provider_readback
tests/test_int21_threads_transport.py::test_known_id_with_wrong_parent_remains_unresolved
tests/test_int21_threads_transport.py::test_reconciliation_cannot_substitute_a_different_publication_id
tests/test_int21_threads_transport.py::test_failed_account_preflight_has_proven_no_write[profile_reply0]
tests/test_int21_threads_transport.py::test_failed_account_preflight_has_proven_no_write[profile_reply1]
tests/test_int21_threads_transport.py::test_failed_account_preflight_has_proven_no_write[profile_reply2]
tests/test_int21_threads_transport.py::test_terminal_page_after_cursor_does_not_fetch_again
tests/test_int21_threads_transport.py::test_publication_id_is_durable_before_readback_interruption
tests/test_int21_threads_transport.py::test_sending_evidence_rejects_invalid_publication_id
tests/test_int21_threads_transport.py::test_http_adapter_enforces_total_deadline_and_one_inflight_request
tests/test_int21_threads_transport.py::test_http_adapter_worker_error_and_start_failure_are_bounded
tests/test_int21_threads_transport.py::test_forward_reply_poll_reaches_new_arrivals_after_saved_cursor
tests/test_int21_threads_transport.py::test_store_rejects_ambiguous_cursor_reset[True-cursor-1]
tests/test_int21_threads_transport.py::test_store_rejects_ambiguous_cursor_reset[1-None]
tests/test_int21_threads_transport.py::test_crash_during_account_preflight_recovers_without_unknown[container]
tests/test_int21_threads_transport.py::test_crash_during_account_preflight_recovers_without_unknown[publish]
tests/test_int21_threads_transport.py::test_live_pages_stop_at_event_budget_and_resume_without_loss
tests/test_int21_threads_transport.py::test_owned_reply_is_skipped_and_cursor_advances
tests/test_int21_threads_transport.py::test_live_page_limit_is_validated_before_http[0]
tests/test_int21_threads_transport.py::test_live_page_limit_is_validated_before_http[11]
tests/test_int21_threads_transport.py::test_live_page_limit_is_validated_before_http[True]
tests/test_int21_threads_transport.py::test_provider_page_exceeding_requested_limit_keeps_cursor
tests/test_int21_threads_transport.py::test_full_page_of_long_valid_text_fits_response_bound_and_advances
tests/test_int21_threads_transport.py::test_media_only_item_is_skipped_and_cursor_advances[reply]
tests/test_int21_threads_transport.py::test_media_only_item_is_skipped_and_cursor_advances[mention]
tests/test_int21_threads_transport.py::test_missing_text_without_supported_media_type_fails_closed[None]
tests/test_int21_threads_transport.py::test_missing_text_without_supported_media_type_fails_closed[TEXT_POST]
tests/test_int21_threads_transport.py::test_missing_text_without_supported_media_type_fails_closed[UNRECOGNIZED]
tests/test_int21_threads_transport.py::test_stop_during_account_preflight_is_rechecked_before_write
```
