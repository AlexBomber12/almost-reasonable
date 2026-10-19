#!/usr/bin/env bash
set -euo pipefail

if [[ $# -gt 1 ]]; then
  printf 'usage: %s [REPOSITORY]\n' "$0" >&2
  exit 2
fi

repo=$(git -C "${1:-.}" rev-parse --show-toplevel) || {
  printf 'Secret scan requires a Git repository\n' >&2
  exit 2
}
if [[ $(git -C "$repo" rev-parse --is-shallow-repository) != false ]]; then
  printf 'Secret scan requires a complete checkout (fetch-depth: 0)\n' >&2
  exit 2
fi

scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
"$(dirname "$0")/install-gitleaks.sh" "$scratch/bin" || {
  printf 'Could not install verified Gitleaks release\n' >&2
  exit 2
}

# The explicit config keeps built-in rules independent of repository and
# environment settings. The bare mirror has no working-tree ignore file.
printf '[extend]\nuseDefault = true\n' > "$scratch/default.toml"
: > "$scratch/empty-ignore"
git clone --quiet --mirror --no-hardlinks "$repo" "$scratch/scan.git" > /dev/null 2>&1 || {
  printf 'Could not prepare full-history scan\n' >&2
  exit 2
}
git -C "$scratch/scan.git" fetch --quiet --no-tags "$repo" HEAD:refs/scan/head > /dev/null 2>&1 || {
  printf 'Could not include evaluated HEAD in scan\n' >&2
  exit 2
}
cat > "$scratch/metadata.tmpl" <<'TEMPLATE'
{{ range . }}rule={{ printf "%q" .RuleID }} file_sha256={{ printf "%q" (sha256sum .File) }} line={{ .StartLine }} commit={{ printf "%q" .Commit }}
{{ end }}
TEMPLATE

status=0
# Keep scanner streams private. The template emits metadata only and hashes
# entire paths, which may themselves contain a different credential.
"$scratch/bin/gitleaks" git "$scratch/scan.git" \
  --config "$scratch/default.toml" \
  --gitleaks-ignore-path "$scratch/empty-ignore" \
  --ignore-gitleaks-allow \
  --log-opts='--all refs/scan/head' \
  --no-banner --redact=100 \
  --report-format=template \
  --report-template "$scratch/metadata.tmpl" \
  --report-path "$scratch/metadata.txt" \
  > /dev/null 2>&1 || status=$?

if [[ $status -eq 0 ]]; then
  printf 'PASS: no Gitleaks findings in all fetched refs and HEAD\n'
  exit 0
fi
if [[ $status -eq 1 && -s "$scratch/metadata.txt" ]]; then
  printf 'FAIL: Gitleaks findings (metadata only):\n'
  cat "$scratch/metadata.txt"
  exit 1
fi
printf 'FAIL: Gitleaks scan could not complete (scanner output withheld)\n' >&2
exit 2
