# Standing private conversations with a monthly budget

The operator may separately authorize `private-standing-grant-v1` for the private
Sol worker. This is an explicit change of authority, never an automatic extension
of an expired ordinary grant. It uses the same closed fields as the ordinary
grant, with `expires_at: null`, `model: "gpt-6.1-sol"`, and
`max_operations: 9223372036854775807`. The existing operation ledger remains a
durable replay fence; this large integer is a storage bound, not prepaid credit.
`not_before`, clock high-water checks, protected-file binding, retirement,
worker Stop, owner Stop and separate capability permissions remain enforced.
Finite and acceptance grants retain their original expiry and operation rules.

Standing authority **requires** `--monthly-budget /worker/PRIVATE-BUDGET.sqlite`.
The worker refuses startup without an existing, intact private wallet before
opening credentials. It never initializes a wallet or creates replacement credit
on restart. This wallet covers both participants' replies and every paid private
extraction, memory, narrative and shared-proposal call. It grants no hosted search
or publication permission.

The `private_sol` lane is fixed at **$10 per calendar month UTC**, separate from
the public Sol $15 and public research $5 wallets, even when the API project and
credentials are shared. A wallet is bound to its lane, project and conservative
price policy. Public wallets and their existing policy encodings are unchanged.
This is application admission accounting, not the provider's delayed invoice or
a cap on unrelated callers using the same API project.

Before each SDK request, an atomic transaction reserves $1 for the bounded Sol
request: no tools, at most 2048 output tokens, default service tier, bounded UTF-8
request size, no streaming/background execution or automatic truncation. Complete
valid usage settles at conservative uncached prices. Errors, missing/inconsistent
usage and lost outcomes retain the entire reservation, including across month
boundaries. There is no automatic refund or reset of unknown holds. A request may
therefore pause before the displayed spend is exactly $10. Ordinary settled spend
becomes available again the next UTC month without replacing the authority,
restarting the worker, replaying an old message or clearing a provider suspension.
The budget itself never records a persistent provider spend suspension.

## Explicit installation and recovery

Take a consistent protected backup and quiesce only private writers under the
private native Stop. Preserve the existing selected allowance, provider usage,
private events, account records and every consumed/replay/hold row. Do not replace
public state or assert a global Stop simply to perform this private cutover.

As the private worker UID, without provider credentials mounted:

```sh
python -B -m app.ops.private_pilot_ops plan --activation /control/STANDING.json
python -B -m app.ops.private_pilot_ops prepare-budget \
  --activation /control/STANDING.json --monthly-budget /worker/PRIVATE-BUDGET.sqlite
python -B -m app.ops.private_pilot_ops replace \
  --activation /control/STANDING.json --allowance /worker/EXISTING-ALLOWANCE.sqlite
```

`prepare-budget` is an exclusive one-time operator action. It refuses finite
authority and an existing wallet. Never rerun it to recover from missing,
replaced or damaged budget state. Restore and reconcile protected state instead.
The first new wallet accounts only calls after cutover; preserve earlier telemetry
and explicitly document the installation time, rather than claiming historical
provider spend has been imported. Subsequent grant replacement reuses that same
wallet and cannot replenish a month's allowance.

Keep the same capability versions and scopes, binding their protected copies to
the new standing pilot ID and storage bound; update the application's narrative
permission and pilot ID. Only the private worker needs the new budget flag.
Verify schema, authority, wallet, permissions, history preservation, IPC peer UID,
login health and unchanged public containers before releasing only the operator's
own maintenance Stop. An HTTP 200 is not proof of a model answer; a participant's
new message supplies that behavioral check. Do not issue a paid diagnostic probe
or replay earlier rejected messages automatically.

```sh
python -B -m app.ops.private_pilot_ops inspect \
  --activation /control/STANDING.json --allowance /worker/EXISTING-ALLOWANCE.sqlite \
  --monthly-budget /worker/PRIVATE-BUDGET.sqlite --stop /control/emergency-stop.json \
  --provider-database /worker/provider.sqlite3
```

Standing inspection includes the month, conservative charged/reserved amount and
limit; `ready` is false without usable budget. Stop remains immediately effective.
Explicitly selecting another operator-approved grant retires standing authority;
restarting or restoring an old activation cannot reactivate a retired grant.

Offline tests exercise native private worker IPC, real allowance/wallet SQLite
transactions, concurrent admission, restart/replay, month rollover, unavailable
state, contract restrictions and Stop. Fake SDK responses establish these control
boundaries; they are not real-model usefulness, human acceptance or a live launch.
