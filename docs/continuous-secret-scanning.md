# Continuous secret scanning

The separate **Secret scan** workflow runs after the existing `CI` workflow
completes for each pull request, and on pushes to `main`. GitHub runs the
[`workflow_run` definition from the default branch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run),
so a PR cannot replace the trusted scanner or publisher job. The scanner job
has read-only `contents`, `actions`, and `pull-requests` permissions. A separate
publisher job has those read permissions plus `checks: write`; it never checks
out candidate code. It uses GitHub's [check-run API](https://docs.github.com/en/rest/checks/runs)
to publish the stable **Trusted secret history scan**
check on the validated PR head SHA. The check is created as in progress and
completed only after the trusted scanner result and current PR head are checked.
The publisher never uses artifacts from the PR's CI run as scan evidence.

The default-branch `workflow_run` path and its published check are authoritative
for pull requests. The scanner uses the standalone [Gitleaks v8.30.1
release](https://github.com/gitleaks/gitleaks/releases/tag/v8.30.1), with the
built-in rules and no license or production credentials. The installer downloads
`gitleaks_8.30.1_linux_x64.tar.gz` from that release and checks SHA-256
`551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`
before extracting or executing it. This value matches the release's published
checksums file and GitHub release-asset digest. To verify it independently:

```sh
curl -fsSLo /tmp/gitleaks_8.30.1_linux_x64.tar.gz \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz
sha256sum /tmp/gitleaks_8.30.1_linux_x64.tar.gz
```

From a complete local checkout, run:

```sh
bash scripts/security/test-secret-scan.sh
node --test scripts/security/test-trusted-check.mjs
bash scripts/security/scan-secrets.sh
```

The scripts need Git, Bash, curl, `sha256sum`, tar, OpenSSL (for synthetic test
data), grep (for the tests), and network access to the pinned release. The
scanner accepts an optional repository path. The publisher regression tests
also need Node.js; they mock GitHub API calls and use no token. A shallow
checkout fails rather than silently scanning only recent commits.

The workflow checks out the trusted default-branch revision and the triggering
CI run's PR head commit (or the pushed `main` commit) separately, with
`fetch-depth: 0`, and verifies that the evaluated checkout's `HEAD` equals the
resolved SHA. It runs the trusted revision's scanner against the evaluated
checkout (including public fork checkouts), regardless of whether the triggering
CI run passed. The scanner and publisher scripts always come from the trusted
default-branch revision; candidate files are only scan input. If the trusted
scripts or resolved PR repository and SHA are missing, the scan fails rather
than falling back to candidate code or the `main` checkout.

Before the scan, trusted code verifies the triggering repository, completed
`CI` workflow identity and path, run attempt, source repository, PR base, head
repository, and exact SHA through the GitHub API. It can find a fork PR through
the open-PR API when `workflow_run.pull_requests` is empty. Missing, ambiguous,
or inconsistent identity fails closed. The publisher independently rechecks that
metadata and the live PR head. If the head changed, it marks the old SHA's
check cancelled; it never marks the newer SHA successful. GitHub may refresh a
completed run's PR entry to the newer head, so the resolver compares that entry
with both the immutable run SHA and the live PR before classifying it as stale.
Duplicate events cannot upgrade an earlier failure, and an older run cannot
overwrite a newer run's result on the same SHA. A skipped, failed, or cancelled
scanner job, missing scan evidence, or API error cannot produce a new successful
check.
A whole workflow cancellation may leave no published result, which also is not
success.

The currently installed default-branch workflow publishes **Trusted secret
history scan** on the PR's exact head SHA. This cleanup takes effect only after
it is merged to `main`; checks on this PR exercise the existing default-branch
workflow. After merge, verify a new PR commit has the authoritative check and
no obsolete direct `pull_request` secret-scan jobs. Select the exact check name,
from the GitHub Actions app, as a required check in the `main` branch protection
or ruleset if merge gating is desired. A check name alone cannot prevent an
actor authorized to change repository workflows or settings from spoofing or
disabling it.

The scanner makes a temporary bare mirror of all
refs available in that checkout, adds the checked-out HEAD as a temporary ref,
and scans `git log -p --all refs/scan/head`. This includes fetched branch and tag
history and the evaluated PR commit, even when the PR commit is detached. A
candidate introduced and later deleted remains visible in its introducing
commit. Refs or objects absent from the checkout are outside this scan; this is
not a scan of unreachable Git objects or every fork's private history.

The scanner forces the built-in rules through an explicit minimal config and
scans the bare mirror, so a repository `.gitleaks.toml` or `.gitleaksignore`
cannot replace rules or suppress findings. It also disables `gitleaks:allow`
comments. There are no path, test, documentation, commit, or baseline
exclusions. Findings fail the check. Scanner stdout and stderr are withheld;
only rule ID, SHA-256 of the path, line, and commit from a metadata-only report
appear in output. The path itself is never printed because it could contain an
unrelated credential. No raw report is uploaded, and temporary files are
removed when the script exits. To resolve a path hash locally without pasting
filenames into CI logs, substitute the reported commit and hash below:

```sh
git ls-tree -rz --name-only COMMIT | python3 -c '
import hashlib, sys
wanted = sys.argv[1]
for path in sys.stdin.buffer.read().split(b"\0"):
    if path and hashlib.sha256(path).hexdigest() == wanted:
        print(path.decode("utf-8", "surrogateescape"))
' PATH_SHA256
```

If a real finding appears, treat the value as exposed: revoke or rotate it at
its provider, investigate use and scope, and remove it from source and history
under an agreed remediation plan. Deleting it in a later commit alone does not
clear this full-history check. Do not paste the value into logs, issues, or PR
discussion. CI detects exposure **after** a push; it does not replace
provider-side revocation or GitHub push protection.
