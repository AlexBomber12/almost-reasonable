# B03E combined memory and narrative acceptance

B03D's accepted run `dd9aa4a81ec4404ba0e03c8479d8cd45` and its protected artifacts remain untouched. B03E uses the existing isolated HTTPS origin, Compose project pattern, protected ledgers and capture wrapper. This repository change authorizes no paid call or deployment.

## Authority and accounting

Use activation `private-pilot-activation-b03e-v1` with task `synthetic-private-combined-memory-b03e`, exactly 20 operations, one attempt per operation, and the existing 512 output token bound. The protected application and worker copies of `private-narrative-permission-v2` contain the same pilot ID and `max_operations: 20`. In v2, that field caps **all additional operations together**: generic evolution, typed memory, shared proposal and narrative proposal. Replies use the activation's separate total cap. For this journey, six evolution, six typed, two narrative and six reply attempts consume exactly 20 global slots. Narrative proposals reserve their matching reply slot atomically. A failed proposal may leave a consumed reply hold without a billed reply; the report identifies the bounded rejection and retains that reservation. The ordinary pilot and B03D activation limits remain unchanged. The v1 narrative permission remains a one-operation opt-in; B03E uses v2 only.

The worker copy lives in its `11021`-owned, mode `0700` control directory; the app copy lives in its `11022`-owned, mode `0700` state directory. Both files are mode `0400`. The app's Compose mounts expose only its own state and the IPC socket. They do not mount the worker credential directory, provider database, allowance database or worker control directory. Both processes read their current copies on each relevant operation. The worker additionally binds the limit to the current activation and allowance ledger.

The provider's narrative Structured Output is `private_narrative_proposal_v2`: an object containing a `proposal` union. The create, revise and no-change branches have mutually consistent nullability. The worker validates UTF-8 size, handle format and provenance locally, and records bounded diagnostic codes on rejected billed responses. The internal worker IPC proposal stays unchanged.

## Later authorized run

After separate approval of the exact reviewed commit, billing scope, activation window and aggregate token/cost limits, prepare protected authorization and activation files with the task and version above. Reuse the isolated HTTPS route and preflight described in [INT-22 private pilot](int22-private-pilot.md). From the clean reviewed checkout on AI-Server, run once:

```sh
sudo python3 -m acceptance.private_pilot_live \
  --combined-journey \
  --authorization /protected/b03e-authorization.json \
  --activation /protected/b03e-activation.json \
  --state-root /srv/almost-reasonable/synthetic-b03e \
  --aggregate-ledger /srv/almost-reasonable/synthetic-b03e-aggregate.sqlite3 \
  --origin https://approved-synthetic-host.example \
  --backend-subnet "$ACCEPTANCE_SUBNET" \
  --backend-ip "$ACCEPTANCE_IP" \
  --report /protected/b03e-report.json
```

The runner creates only a synthetic account and executes the six fixed Russian turns. It restarts the app before turn 4 and deletes the initiating narrative source before turn 6 through authenticated privacy deletion. The one-event history bound forces restart recall through persistent memory. The report records exact provider attempts, allowance, safe diagnostics, both Stops, technical status and the synthetic username. A mode `0600` transcript at `transcript_path` contains the original story, continuation, restart recall and reality answer for human semantic review. Technical `passed` means the structural, persistence, privacy and accounting checks passed; `human_semantic_review` remains `pending`. Human reviewers must judge narrative continuity and truthful disclosure from the actual text; keyword checks do not approve semantics or personality fit.

The fiction policy is unchanged: general AI/fiction disclosure at profile/chat entry permits first-person invented memories. A direct question about reality calls for a truthful answer. Fiction is never evidence of actual actions or credentials.

## Inspect the stopped synthetic conversation

After a successful run, keep the existing HTTPS infrastructure and both Stops enabled, and keep `provider-worker` stopped. Serve only the saved synthetic app state:

```sh
sudo scripts/serve_b03e_synthetic.sh /protected/b03e-report.json \
  /srv/almost-reasonable/synthetic-b03e \
  https://approved-synthetic-host.example \
  "$ACCEPTANCE_SUBNET" "$ACCEPTANCE_IP"
```

The command requires the clean checkout recorded in the protected report and its recorded application image ID to be available locally. It will not rebuild or pull an image. The command prints the non-secret username `synthetic-a-<12 hex characters>` from the protected report. Use the synthetic password entered for the run. On desktop and a physical iPhone, check keyboard and composer visibility, message order after refresh, scrolling, draft/error behavior, AI/fiction disclosure, privacy deletion state and logout. Browser emulation is useful for layout checks but is not physical iPhone acceptance. Stop `private-app` after inspection; preserve the protected transcript, report, ledgers and B03D artifacts. No new proxy, domain or dashboard is needed.
