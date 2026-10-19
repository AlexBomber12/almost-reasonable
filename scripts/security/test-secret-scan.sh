#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

new_repo() {
  local name=$1
  mkdir -p "$scratch/$name"
  git -C "$scratch/$name" init -q
  git -C "$scratch/$name" config user.email scan-test@example.invalid
  git -C "$scratch/$name" config user.name 'Scan test'
  printf 'clean\n' > "$scratch/$name/README.md"
  git -C "$scratch/$name" add README.md
  git -C "$scratch/$name" commit -qm initial
}

scan() {
  local name=$1
  local expected=$2 status=0
  "$root/scripts/security/scan-secrets.sh" "$scratch/$name" > "$scratch/$name.log" 2>&1 || status=$?
  if [[ $status -ne $expected ]]; then
    printf 'FAIL: %s scan returned %s, expected %s\n' "$name" "$status" "$expected" >&2
    exit 1
  fi
}

assert_hidden() {
  local name=$1
  local value=${2:-$candidate}
  if grep -Fq -- "$value" "$scratch/$name.log"; then
    printf 'FAIL: candidate disclosed in %s log\n' "$name" >&2
    exit 1
  fi
}

# Construct a high-entropy candidate only inside the temporary test repository.
candidate="ghp_$(openssl rand -base64 48 | tr -cd 'A-Za-z0-9' | head -c 36)"

new_repo clean
scan clean 0

new_repo present
printf 'token = %s\n' "$candidate" > "$scratch/present/credential.txt"
git -C "$scratch/present" add credential.txt
git -C "$scratch/present" commit -qm add-candidate
branch=$(git -C "$scratch/present" branch --show-current)
git -C "$scratch/present" switch --detach -q
git -C "$scratch/present" branch -D "$branch" > /dev/null
scan present 1
assert_hidden present
file_id=$(printf 'credential.txt' | sha256sum | cut -d ' ' -f 1)
grep -Fq "rule=\"github-pat\" file_sha256=\"$file_id\" line=1" "$scratch/present.log" || {
  printf 'FAIL: missing rule and location in finding\n' >&2; exit 1;
}

new_repo pathleak
other_candidate="ghp_$(openssl rand -base64 48 | tr -cd 'A-Za-z0-9' | head -c 36)"
printf 'token = %s\n' "$other_candidate" > "$scratch/pathleak/$candidate.txt"
git -C "$scratch/pathleak" add .
git -C "$scratch/pathleak" commit -qm add-candidate
scan pathleak 1
assert_hidden pathleak
assert_hidden pathleak "$other_candidate"
grep -Eq 'file_sha256="[a-f0-9]{64}"' "$scratch/pathleak.log" || {
  printf 'FAIL: missing hashed location\n' >&2; exit 1;
}

new_repo removed
printf 'token = %s\n' "$candidate" > "$scratch/removed/credential.txt"
git -C "$scratch/removed" add credential.txt
git -C "$scratch/removed" commit -qm add-candidate
git -C "$scratch/removed" rm -q credential.txt
git -C "$scratch/removed" commit -qm remove-candidate
scan removed 1
assert_hidden removed

new_repo ignores
printf 'token = %s # gitleaks:allow\n' "$candidate" > "$scratch/ignores/credential.txt"
git -C "$scratch/ignores" add credential.txt
git -C "$scratch/ignores" commit -qm add-candidate
scan ignores 1
assert_hidden ignores
commit=$(git -C "$scratch/ignores" rev-parse HEAD)
printf '%s:credential.txt:github-pat:1\n' "$commit" > "$scratch/ignores/.gitleaksignore"
git -C "$scratch/ignores" add .gitleaksignore
git -C "$scratch/ignores" commit -qm add-ignore-file
scan ignores 1
assert_hidden ignores
printf '[allowlist]\nregexes = [".*"]\n' > "$scratch/ignores/.gitleaks.toml"
git -C "$scratch/ignores" add .gitleaks.toml
git -C "$scratch/ignores" commit -qm add-config-file
scan ignores 1
assert_hidden ignores

printf 'PASS: clean, present, detached, removed, ignored, and redaction cases\n'
