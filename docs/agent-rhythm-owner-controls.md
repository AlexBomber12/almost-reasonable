# Bounded rhythm and integrated owner controls

This document describes the scheduling and control portion of Stage 8. The
public-to-private integration and its protected restore/deletion ancestry are
described in [public-private-intellectual-provenance.md](public-private-intellectual-provenance.md).
Real-model M1–M6 and SCN01–12 acceptance remain separate from deterministic
implementation checks.

## Agent-selected rhythm

`enable_agent_cadence` in the protected public runtime configuration defaults to
false. When enabled, the actual SDK sees a bounded `capability_snapshot` and can
propose an intrinsic `cadence_preference` with an integer `seconds` field. The
minimum is the ceiling of the owner's existing cadence, at least one second;
the maximum is 24 hours. The preference grants no tools, posting, extra model
calls, research or spending. Existing finite window and provider budgets remain.

Only a current, source-valid autonomous wakeup inside a finite operating window
can apply its accepted preference. Incoming requests cannot directly schedule
work. Other intellectual changes and no preference leave the interval alone.
A preference below the owner floor is recorded as a wish without changing the
schedule. The current typed prompt metadata is `agent-intellectual-state-prompt-v4`;
the composed free-mission intellectual prompt is version 2.

Scheduler choices are durable, operation-bound, idempotent and reject older
decision times. The next wakeup respects the last actual deliberation plus the
selected interval, including after restart.
The finite window recomputes its wakeup deadline when the selected interval
changes, including a shorter interval and recovery after the scheduler committed
before the window. Replaying an old preference cannot undo a trusted owner reset.
An active preference retains its source lease. Source withdrawal, policy changes
or disabling the mode hold
further work until an explicit trusted scheduler reset. No automatic reset
increases activity, and no publication is required.

## Owner control and observation

The existing authenticated owner console accepts `--worker-registry PATH`.
Its owner-owned config file has mode 0400 in a mode 0700 directory, version
`owner-worker-registry-v1`, and a `workers` list of one to eight exact targets.
Each entry contains `name` (at most 32 characters), absolute `telemetry_socket`
and `stop_socket` paths, and the expected `uid`. Configure every actual worker;
an omitted service has no claimed observation or Stop coverage.

The owner UI shows each configured worker's live availability, native Stop,
recorded attempts, recorded tokens and unknown usage. These are observations,
not authoritative billing. An emergency command first asserts application Stop,
then independently asserts each worker's Stop. Only intact acknowledgements
count as confirmed. Missing or uncertain targets remain visible. Per-target
results persist, and a retry of the same operation does not repeat confirmed
targets. The operation binds the complete endpoint configuration; configuration
replacement requires a fresh authenticated operation. Existing owner login,
origin, CSRF and session boundaries apply.

The emergency control remains available until application Stop and every
configured worker have fresh, valid Stop observations. A stopped primary worker
cannot hide the control while another target is active, unavailable or stale.

`app.trusted.worker_stop` and `app.trusted.worker_telemetry` accept an explicit
`--stop-version` from the closed private-pilot, public-model and public-research
formats. The default remains the private-pilot format. Stop assertion preserves
the exact format, syncs it durably, and offers no remote release command. The
public model worker's protected configuration now requires `stop_file` before
provider credentials are read. It checks native Stop before admission, again
before SDK inference, and before returning a completed result. In-flight model
cost is not cancelled; a pre-SDK interrupted reservation remains conservative.
The research worker already checks its native Stop.

The owner console also accepts `--public-runtime-config PATH`. The protected
file has version `public-runtime-observation-v1` and exact fields `account`,
`journal_path`, `threads_path`, `ledger_path`, `owner_directory`, and
`scheduler_path` (which may be null). Public and private applications must share
the same owner Stop store. The read-only public binding checks the public
application's recorded owner directory against the console's directory. It
opens no provider credential and makes no HTTP request. Missing databases are
never initialized. The UI shows bounded recent decision categories, stored
window counts, finite grant consumption and the observed effective cadence;
it exposes no draft text, source text, private history, token or storage path.

## Evidence and remaining work

Offline tests exercise actual local worker IPC, native peer credentials, durable
journals, shared application Stop, multi-worker acknowledgements, malformed
configuration, source withdrawal, cadence recovery and Chromium owner UI at
desktop/mobile widths. SDK and provider HTTP are fake. This proves deterministic
implementation behavior only.

Public-to-private intellectual influence now has the protected dependency
representation, erasure closure and quarantined restore support described in
[its source protocol](public-private-intellectual-provenance.md). It still needs
explicit reviewed deployment wiring. Real-model acceptance, two actual participants, live Meta
permissions and launch remain separate work. Preparing this PR activates no
grant, paid provider call, deployment, enrollment or real public post.
