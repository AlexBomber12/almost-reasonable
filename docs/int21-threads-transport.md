# INT-21: Threads transport contract and offline evidence

B11A rechecked the read contracts on 2026-10-26 against Meta's current developer guides, references and changelog. See the [INT-27 evidence and operator runbook](int27-threads-readonly-probe.md) for exact references, access limitations, request fields and remaining real-account prerequisites. The earlier 2026-10-16 offline publication evidence used Meta's official [Postman collection](https://www.postman.com/meta/threads/documentation/dht3nzz/threads-api); this read-contract task does not extend publication validation. No live account, token, App Review approval or granted scope was inspected.

## Supported offline contract

| Operation | Meta endpoint | Required scope evidence in local policy | Accepted evidence |
| --- | --- | --- | --- |
| Account binding | `GET /me?fields=id` | `threads_basic` | ID equals configured Meta user ID |
| Text container | `POST /{user}/threads` | `threads_basic`, `threads_content_publish` | Returned container `id` |
| Publish container | `POST /{user}/threads_publish` | `threads_basic`, `threads_content_publish` | Returned post `id`, then `GET /{id}` owner/parent check |
| Replies to configured root | `GET /{root}/replies` | `threads_basic`, `threads_read_replies` | Complete parsed page with both `root_post.id` and `replied_to.id` equal to the configured root, and a valid cursor |
| Mentions of configured user | `GET /{user}/mentions` | `threads_basic`, `threads_manage_mentions` | Documented post fields with provider IDs and a valid cursor; ID route explicitly supported |

The scopes above are service policy checks, not claims that the Meta app or current token actually has those permissions. The official developer references document the read routes; the supplementary collection documents the two-step container/publish flow. It does not establish a safe retry rule for a lost write response or a numeric rate quota for this specific account. HTTP 429 therefore remains uncertain after a write; an offline fixture does not establish live rate behavior.

- A trusted, explicit `ThreadsHTTPTransport` binds the local account, Meta user ID, root post ID, token and HTTP client. The destination and version are fixed to `https://graph.threads.net/v1.0`; no caller can supply a URL. OAuth bearer credentials stay in the authorization header. The standard HTTPS client verifies TLS, disables redirects, caps each response at 2 MiB, and returns or times out within the configured total caller deadline of at most 10 seconds. Real inbound requests use at most 10 items per page, leaving room within that response bound for fully escaped text at the 16 KiB per-item input limit. Each page's HTTP timeout is also clamped to the remaining service poll deadline. A timed-out write may still finish in its background worker and is classified uncertain; one in-flight request per transport prevents accumulating workers. The completed worker releases its request slot before exposing the result to the caller. The service accepts this transport only with the Boolean `live_enabled=True` and a Stage 3 binding; non-live construction requires the exact `ThreadsSimulator`, excluding real transport subclasses. The service pins the original transport object, activation flag, Meta user ID, reply root and source reference at construction and rejects later rebinding before effects. Stage 0 still admits only the exact `ThreadsSimulator`.
- `GET /me?fields=id` checks the authenticated account before each write and before the durable write-dispatch marker. Approval, privacy and stop controls are rechecked after that read and before the marker. A failed or interrupted preflight read is a proven no-write result. `POST /{user}/threads` creates a `TEXT` container, with `reply_to_id` for a reply. `POST /{user}/threads_publish` publishes its `creation_id`. The returned publication ID is stored durably before `GET /{id}` checks the same owner and expected parent; a malformed present parent cannot confirm a top-level post. Actual returned container and publication IDs use existing columns, so no schema migration is needed.
- Structured 400, 401 and 403 Graph errors reject an operation. A structured 429, server error, redirect, missing ID, malformed or oversized response, interrupted request, or failed publication verification leaves the operation `UNKNOWN` when a write may have occurred. The account remains paused. Current approval, per-draft privacy, policy, stop, capability and outbox reservations remain at the existing service boundary. A live service requires the current policy stage to remain Stage 3 before dispatch, even if simulation is enabled later. An explicit pre-transmission failure may use the existing bounded retry path.
- `GET /{root}/replies` requests `reverse=false` for chronological pages, so a durable `after` cursor can reach later arrivals. Account-owned replies are validated and skipped while the page cursor advances. Valid image, video and carousel items without text are skipped after source and routing checks; the page cursor still advances. `GET /{user}/mentions` has no verified forward-order parameter in the official collection, so a completed bounded scan resets its durable cursor and the next poll starts at the head; interrupted scans continue from their saved `after` cursor. `paging.cursors.after` controls continuation independently of links; the documented post/mention pages omit next/previous links and reply examples also use cursors alone. A page with no `after` cursor ends the scan. Malformed present cursors fail closed. Provider `after` values are opaque and preserved in full; their storage bound is derived from the 2 MiB response bound rather than an assumed provider cursor length. Each real page request is capped by the remaining service event budget; the service stops fetching when that budget is full and commits the bounded prefix. The transport validates every event before returning a verified page. Present mention relationship objects must contain valid provider IDs; malformed relationships fail the page without advancing its cursor. The service commits all fetched pages, events and the final cursor in one transaction. A malformed later page commits none of them. Reply routing requires both the root and immediate parent to match the configured top-level root. Mention requests use only `id,text,media_type,timestamp`; reply-only fields are excluded. Nested mention relationship discovery remains unavailable under the documented post field contract. Unsolicited relationship objects still require valid IDs. Mention records keep provider IDs. Cursor digest/encoding and saved values are unchanged; no reset or migration is introduced.

## Deliberately unresolved

The published contract does not provide an operation key that proves a lost write response did or did not create exactly this publication. If the publish response yielded a real post ID before readback failed, reconciliation can query that exact ID and confirm only when owner and parent bindings match. Otherwise `reconcile` returns `UNRESOLVED`; it never searches by text or time, synthesizes a provider ID, republishes, or authorizes a replacement. Administrative `UNKNOWN_ABANDONED` remains the existing purpose-bound path. A separately reviewed strategy with exact provider evidence is needed to resolve effects whose response ID was lost.

The Meta changelog (June 7, 2024) specifies `v1.0` on the existing domain, and the June 6, 2025 entry confirms that domain remains supported. Real-token acceptance and bearer-header behavior still require the authorized read probe before activation. The Media Retrieval reference lists the publication readback fields, but the post guide limits `owner` to owned top-level posts; reply-publication owner readback remains an unresolved publishing prerequisite. Publication behavior is unchanged. App review, current account scopes, token validity, publishing limits, endpoint field availability, and mention/reply coverage cannot be established from requested scopes or offline responses. Public keyword search, token refresh, profile edits, general model-generated publication, deployment configuration and an unauthenticated publish endpoint are outside this milestone. The fixed root ID limits reply polling to one configured conversation; broader discovery needs a separate reviewed design.

The mention head-rescan strategy avoids treating a reverse-ordered `after` cursor as a permanent checkpoint. Under sustained arrivals faster than the bounded page budget, a scan may take multiple polls to reach its terminal page; operational backlog limits and Meta's actual ordering require live validation before activation. Live cursors contain a digest of the source reference, provider user ID, stream and stream route. On activation from simulation, or after a provider user or reply root change, the service starts at the first page and atomically replaces or clears the old cursor with the newly verified result. Stage 3 readiness recognizes an explicitly enabled live transport.

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
| `test_live_cursor_is_bound_to_stream_and_provider_route` | `INT-21-CURSOR-ROUTE-BINDING` |
| `test_simulator_cursor_is_reset_when_live_transport_activates` | `INT-21-CURSOR-MIGRATION` |
| `test_changed_live_reply_route_restarts_from_first_page` | `INT-21-ROUTE-CHANGE` |
| `test_live_stage_3_readiness_reports_enabled_transport` | `INT-21-LIVE-READINESS` |
| `test_account_preflight_prevents_publication_to_wrong_profile` | `INT-21-ACCOUNT-PREFLIGHT` |
| `test_explicit_live_binding_requires_real_transport_and_stage_3` | `INT-21-LIVE-BINDING` |
| `test_later_malformed_page_does_not_commit_earlier_events` | `INT-21-PAGE-ROLLBACK` |
| `test_known_publication_id_reconciles_from_exact_provider_readback` | `INT-21-EXACT-ID-RECONCILIATION` |
| `test_known_id_with_wrong_parent_remains_unresolved` | `INT-21-RECONCILIATION-BINDING` |
| `test_reconciliation_cannot_substitute_a_different_publication_id` | `INT-21-RECONCILIATION-IDENTITY` |
| `test_failed_account_preflight_has_proven_no_write` | `INT-21-PREFLIGHT-CLASSIFICATION` |
| `test_cursors_only_pages_continue_without_next_link` | `INT-21-CURSOR-ONLY-PAGES` |
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
| `test_http_adapter_releases_slot_before_result_is_visible` | `INT-21-HTTP-SLOT-ORDERING` |
| `test_live_page_http_timeout_uses_remaining_poll_budget` | `INT-21-POLL-DEADLINE` |
| `test_live_page_rejects_invalid_http_timeout_before_request` | `INT-21-PAGE-DEADLINE-VALIDATION` |
| `test_live_poll_deadline_expiring_before_request_avoids_http` | `INT-21-EXPIRED-POLL-NO-REQUEST` |
| `test_live_activation_rejects_non_boolean_values` | `INT-21-BOOLEAN-LIVE-INTERLOCK` |
| `test_malformed_present_mention_relationship_keeps_cursor` | `INT-21-MENTION-RELATION-VALIDATION` |
| `test_revoked_stage_prevents_live_publication_even_with_simulation_enabled` | `INT-21-LIVE-STAGE-REVOCATION` |
| `test_malformed_publication_parent_cannot_confirm_or_reconcile` | `INT-21-PUBLICATION-PARENT-VALIDATION` |
| `test_rebinding_simulation_service_to_real_transport_cannot_poll` | `INT-21-TRANSPORT-REBIND-POLL` |
| `test_rebinding_live_service_transport_cannot_dispatch` | `INT-21-TRANSPORT-REBIND-DISPATCH` |
| `test_real_transport_subclass_cannot_enter_non_live_service` | `INT-21-NONLIVE-EXACT-SIMULATOR` |
| `test_live_activation_cannot_be_disabled_after_construction` | `INT-21-LIVE-ACTIVATION-IMMUTABLE` |
| `test_long_opaque_provider_cursor_persists_and_resumes` | `INT-21-OPAQUE-CURSOR-LENGTH` |
| `test_oversized_stored_cursor_rejected_before_http` | `INT-21-STORED-CURSOR-BOUND` |
| `test_store_rejects_invalid_bounded_cursor` | `INT-21-CURSOR-STORAGE-BOUND` |
| `test_live_provider_binding_change_prevents_poll` | `INT-21-PINNED-PROVIDER-ROUTING` |
| `test_live_source_mutation_cannot_persist_forged_provenance` | `INT-21-PINNED-SOURCE-PROVENANCE` |

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
tests/test_int21_threads_transport.py::test_malformed_mention_pages_do_not_advance_cursor[payload9]
tests/test_int21_threads_transport.py::test_valid_reply_and_mention_parent_are_preserved
tests/test_int21_threads_transport.py::test_invalid_stored_cursor_and_stream_rejected_before_http
tests/test_int21_threads_transport.py::test_live_cursor_is_bound_to_stream_and_provider_route
tests/test_int21_threads_transport.py::test_simulator_cursor_is_reset_when_live_transport_activates
tests/test_int21_threads_transport.py::test_changed_live_reply_route_restarts_from_first_page
tests/test_int21_threads_transport.py::test_live_stage_3_readiness_reports_enabled_transport
tests/test_int21_threads_transport.py::test_account_preflight_prevents_publication_to_wrong_profile
tests/test_int21_threads_transport.py::test_explicit_live_binding_requires_real_transport_and_stage_3
tests/test_int21_threads_transport.py::test_live_activation_rejects_non_boolean_values[false]
tests/test_int21_threads_transport.py::test_live_activation_rejects_non_boolean_values[1]
tests/test_int21_threads_transport.py::test_live_activation_rejects_non_boolean_values[None]
tests/test_int21_threads_transport.py::test_revoked_stage_prevents_live_publication_even_with_simulation_enabled
tests/test_int21_threads_transport.py::test_rebinding_simulation_service_to_real_transport_cannot_poll
tests/test_int21_threads_transport.py::test_rebinding_live_service_transport_cannot_dispatch
tests/test_int21_threads_transport.py::test_live_provider_binding_change_prevents_poll[reply_thread_id-other-root]
tests/test_int21_threads_transport.py::test_live_provider_binding_change_prevents_poll[provider_user_id-other-user]
tests/test_int21_threads_transport.py::test_live_source_mutation_cannot_persist_forged_provenance
tests/test_int21_threads_transport.py::test_real_transport_subclass_cannot_enter_non_live_service
tests/test_int21_threads_transport.py::test_live_activation_cannot_be_disabled_after_construction
tests/test_int21_threads_transport.py::test_malformed_present_mention_relationship_keeps_cursor[bad-root_post]
tests/test_int21_threads_transport.py::test_malformed_present_mention_relationship_keeps_cursor[bad-replied_to]
tests/test_int21_threads_transport.py::test_malformed_present_mention_relationship_keeps_cursor[value1-root_post]
tests/test_int21_threads_transport.py::test_malformed_present_mention_relationship_keeps_cursor[value1-replied_to]
tests/test_int21_threads_transport.py::test_malformed_present_mention_relationship_keeps_cursor[value2-root_post]
tests/test_int21_threads_transport.py::test_malformed_present_mention_relationship_keeps_cursor[value2-replied_to]
tests/test_int21_threads_transport.py::test_malformed_present_mention_relationship_keeps_cursor[None-root_post]
tests/test_int21_threads_transport.py::test_malformed_present_mention_relationship_keeps_cursor[None-replied_to]
tests/test_int21_threads_transport.py::test_later_malformed_page_does_not_commit_earlier_events
tests/test_int21_threads_transport.py::test_known_publication_id_reconciles_from_exact_provider_readback
tests/test_int21_threads_transport.py::test_known_id_with_wrong_parent_remains_unresolved
tests/test_int21_threads_transport.py::test_malformed_publication_parent_cannot_confirm_or_reconcile[bad]
tests/test_int21_threads_transport.py::test_malformed_publication_parent_cannot_confirm_or_reconcile[relation1]
tests/test_int21_threads_transport.py::test_malformed_publication_parent_cannot_confirm_or_reconcile[relation2]
tests/test_int21_threads_transport.py::test_reconciliation_cannot_substitute_a_different_publication_id
tests/test_int21_threads_transport.py::test_failed_account_preflight_has_proven_no_write[profile_reply0]
tests/test_int21_threads_transport.py::test_failed_account_preflight_has_proven_no_write[profile_reply1]
tests/test_int21_threads_transport.py::test_failed_account_preflight_has_proven_no_write[profile_reply2]
tests/test_int21_threads_transport.py::test_cursors_only_pages_continue_without_next_link[reply]
tests/test_int21_threads_transport.py::test_cursors_only_pages_continue_without_next_link[mention]
tests/test_int21_threads_transport.py::test_publication_id_is_durable_before_readback_interruption
tests/test_int21_threads_transport.py::test_sending_evidence_rejects_invalid_publication_id
tests/test_int21_threads_transport.py::test_http_adapter_enforces_total_deadline_and_one_inflight_request
tests/test_int21_threads_transport.py::test_http_adapter_worker_error_and_start_failure_are_bounded
tests/test_int21_threads_transport.py::test_http_adapter_releases_slot_before_result_is_visible[False]
tests/test_int21_threads_transport.py::test_http_adapter_releases_slot_before_result_is_visible[True]
tests/test_int21_threads_transport.py::test_forward_reply_poll_reaches_new_arrivals_after_saved_cursor
tests/test_int21_threads_transport.py::test_long_opaque_provider_cursor_persists_and_resumes
tests/test_int21_threads_transport.py::test_oversized_stored_cursor_rejected_before_http
tests/test_int21_threads_transport.py::test_store_rejects_invalid_bounded_cursor[oversized]
tests/test_int21_threads_transport.py::test_store_rejects_invalid_bounded_cursor[wrong_type]
tests/test_int21_threads_transport.py::test_store_rejects_ambiguous_cursor_reset[True-cursor-1]
tests/test_int21_threads_transport.py::test_store_rejects_ambiguous_cursor_reset[1-None]
tests/test_int21_threads_transport.py::test_crash_during_account_preflight_recovers_without_unknown[container]
tests/test_int21_threads_transport.py::test_crash_during_account_preflight_recovers_without_unknown[publish]
tests/test_int21_threads_transport.py::test_live_pages_stop_at_event_budget_and_resume_without_loss
tests/test_int21_threads_transport.py::test_owned_reply_is_skipped_and_cursor_advances
tests/test_int21_threads_transport.py::test_live_page_limit_is_validated_before_http[0]
tests/test_int21_threads_transport.py::test_live_page_limit_is_validated_before_http[11]
tests/test_int21_threads_transport.py::test_live_page_limit_is_validated_before_http[True]
tests/test_int21_threads_transport.py::test_live_page_http_timeout_uses_remaining_poll_budget
tests/test_int21_threads_transport.py::test_live_poll_deadline_expiring_before_request_avoids_http
tests/test_int21_threads_transport.py::test_live_page_rejects_invalid_http_timeout_before_request[0]
tests/test_int21_threads_transport.py::test_live_page_rejects_invalid_http_timeout_before_request[-1]
tests/test_int21_threads_transport.py::test_live_page_rejects_invalid_http_timeout_before_request[True]
tests/test_int21_threads_transport.py::test_live_page_rejects_invalid_http_timeout_before_request[1]
tests/test_int21_threads_transport.py::test_live_page_rejects_invalid_http_timeout_before_request[31]
tests/test_int21_threads_transport.py::test_provider_page_exceeding_requested_limit_keeps_cursor
tests/test_int21_threads_transport.py::test_full_page_of_long_valid_text_fits_response_bound_and_advances
tests/test_int21_threads_transport.py::test_media_only_item_is_skipped_and_cursor_advances[reply]
tests/test_int21_threads_transport.py::test_media_only_item_is_skipped_and_cursor_advances[mention]
tests/test_int21_threads_transport.py::test_missing_text_without_supported_media_type_fails_closed[None]
tests/test_int21_threads_transport.py::test_missing_text_without_supported_media_type_fails_closed[TEXT_POST]
tests/test_int21_threads_transport.py::test_missing_text_without_supported_media_type_fails_closed[UNRECOGNIZED]
tests/test_int21_threads_transport.py::test_stop_during_account_preflight_is_rechecked_before_write
```
