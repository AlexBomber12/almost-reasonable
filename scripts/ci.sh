#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python -m ruff check .
python -m ruff format --check .
coverage_reports=(--cov-report=term-missing)
if [[ -n "${INT10_COVERAGE_RESULTS:-}" ]]; then
  coverage_reports+=("--cov-report=json:$INT10_COVERAGE_RESULTS")
fi
python -m pytest \
  -p acceptance.plugin \
  --cov=app \
  --cov-branch \
  "${coverage_reports[@]}" \
  --cov-fail-under=100 \
  tests
