#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python -m ruff check .
python -m ruff format --check .
python -m pytest \
  --cov=app \
  --cov-branch \
  --cov-report=term-missing \
  --cov-fail-under=100 \
  tests
