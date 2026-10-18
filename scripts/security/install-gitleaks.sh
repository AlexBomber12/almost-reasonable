#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  printf 'usage: %s DESTINATION_DIRECTORY\n' "$0" >&2
  exit 2
fi

version=8.30.1
archive=gitleaks_${version}_linux_x64.tar.gz
sha256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb
destination=$1
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT

curl --fail --location --silent --show-error --retry 3 \
  "https://github.com/gitleaks/gitleaks/releases/download/v${version}/${archive}" \
  --output "$scratch/$archive"
printf '%s  %s\n' "$sha256" "$scratch/$archive" | sha256sum --check --status || {
  printf 'Gitleaks release checksum verification failed\n' >&2
  exit 1
}

mkdir -p "$destination"
tar -xzf "$scratch/$archive" -C "$scratch" gitleaks
install -m 0755 "$scratch/gitleaks" "$destination/gitleaks"
