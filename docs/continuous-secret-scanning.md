# Continuous secret scanning

The separate **Secret scan** workflow runs on pull requests and pushes to
`main`. It uses the standalone [Gitleaks v8.30.1
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
bash scripts/security/scan-secrets.sh
```

The scripts need Git, Bash, curl, `sha256sum`, tar, OpenSSL (for synthetic test
data), ripgrep (for the tests), and network access to the pinned release. The
scanner accepts an optional repository path. A shallow checkout fails rather
than silently scanning only recent commits.

The workflow checks out the pull request's head commit (or the pushed `main`
commit) with `fetch-depth: 0`. The scanner makes a temporary bare mirror of all
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
only rule ID, quoted path, line, and commit from a metadata-only report appear
in output. A matched value in a path is replaced with `[REDACTED]`. No raw
report is uploaded, and temporary files are removed when the script exits.
Paths and rule IDs are still repository-controlled metadata; do not put
sensitive values in filenames.

If a real finding appears, treat the value as exposed: revoke or rotate it at
its provider, investigate use and scope, and remove it from source and history
under an agreed remediation plan. Deleting it in a later commit alone does not
clear this full-history check. Do not paste the value into logs, issues, or PR
discussion. CI detects exposure **after** a push; it does not replace
provider-side revocation or GitHub push protection.
