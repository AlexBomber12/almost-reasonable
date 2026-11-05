# Diagnosing private request latency

Successful authenticated `/api/messages` and `/api/history` responses include
standard `Server-Timing` durations in milliseconds. Browser Network tools show
`app` for processing through response headers; a model-backed turn also reports
`context` for memory context reservation and `model_stage` for the main inference
stage. Replayed turns and history reads do not fabricate a model duration.

These stages are local application measurements. The model stage includes worker
IPC, admission, inference and response validation, so it is not a provider-only
latency or invoice measurement. Stages can cover only part of total application
time. Browser delivery and later history synchronization remain separate timings.
No speed threshold or production percentile follows from an offline regression.

Only fixed names and numeric durations cross the boundary. Timings are request
local, are not stored or logged, and carry no text, user, operation, source, model
configuration or credential. Concurrent requests and background maintenance cannot
share the collector. Unauthenticated/error HTTP responses, pages and static assets
do not expose this header. Existing authentication, authorization and no-store
response policy remain in effect. This adds no provider call or authority.
