#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 5 || $EUID -ne 0 ]]; then
  echo "usage: sudo scripts/serve_b03e_synthetic.sh REPORT STATE_ROOT HTTPS_ORIGIN BACKEND_SUBNET BACKEND_IP" >&2
  exit 2
fi

repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
for dependency in python3 git docker mktemp; do
  if ! command -v "$dependency" >/dev/null 2>&1; then
    echo "missing host dependency: $dependency" >&2
    exit 1
  fi
done
report="$1"
state_root="$2"
origin="$3"
subnet="$4"
address="$5"

checkout="$(git -c "safe.directory=$repo" rev-parse HEAD)" || exit 1
if ! checkout_status="$(git -c "safe.directory=$repo" status --porcelain)"; then
  echo "could not inspect B03E checkout" >&2
  exit 1
fi
if [[ -n "$checkout_status" ]]; then
  echo "B03E checkout must be clean" >&2
  exit 1
fi
mapfile -t fields < <(python3 - "$report" "$state_root" "$origin" "$checkout" <<'PY'
import json, pathlib, re, sys
from urllib.parse import urlsplit

report = json.loads(pathlib.Path(sys.argv[1]).read_text())
root = pathlib.Path(sys.argv[2]).resolve()
origin = urlsplit(sys.argv[3])
run_id = report.get("run_id", "")
project = report.get("compose_project", "")
username = report.get("synthetic_username", "")
image = report.get("images", {}).get("almost-reasonable-private-pilot-app:local", "")
if (
    report.get("status") != "passed"
    or report.get("technical_journey") != "passed"
    or report.get("worker_stop") is not True
    or report.get("owner_stop") is not True
    or not re.fullmatch(r"[0-9a-f]{32}", run_id)
    or not re.fullmatch(r"almost-reasonable-ship-[0-9a-f]{12}", project)
    or project != "almost-reasonable-ship-" + run_id[:12]
    or not re.fullmatch(r"synthetic-a-[0-9a-f]{12}", username)
    or report.get("commit") != sys.argv[4]
    or not re.fullmatch(r"sha256:[0-9a-f]{64}", image)
    or origin.scheme != "https"
    or not origin.hostname
):
    raise SystemExit("protected B03E report or HTTPS origin is invalid")
stop = json.loads((root / "control" / "emergency-stop.json").read_text())
permission = json.loads((root / "runs" / run_id / "app" / "narrative-permission.json").read_text())
if stop != {"version": "private-pilot-emergency-stop-v1", "stop": True} or permission.get("version") != "private-narrative-permission-v2":
    raise SystemExit("worker Stop or B03E permission is invalid")
for value in (run_id, project, username, permission["pilot_id"], origin.hostname, image):
    print(value)
PY
)
if [[ ${#fields[@]} -ne 6 ]]; then
  echo "protected B03E report validation failed" >&2
  exit 1
fi

run_id="${fields[0]}"
export COMPOSE_PROJECT_NAME="${fields[1]}"
username="${fields[2]}"
export PILOT_ID="${fields[3]}"
export PILOT_ALLOWED_HOST="${fields[4]}"
export PILOT_ALLOWED_ORIGIN="$origin"
export PILOT_APP_SUBNET="$subnet"
export PILOT_APP_IP="$address"
export PILOT_SOCKET_DIR="$state_root/runs/$run_id/ipc"
export PILOT_WORKER_STATE_DIR="$state_root/worker"
export PILOT_APP_STATE_DIR="$state_root/runs/$run_id/app"
export PILOT_CONTROL_DIR="$state_root/control"
export PILOT_SECRET_DIR="$state_root/secrets"

recorded_image="${fields[5]}"
if [[ "$(docker image inspect --format '{{.Id}}' "$recorded_image")" != "$recorded_image" ]]; then
  echo "recorded B03E application image is unavailable" >&2
  exit 1
fi
override="$(mktemp)"
trap 'rm -f -- "$override"' EXIT
printf 'services:\n  private-app:\n    image: %s\n    build: !reset null\n    pull_policy: never\n' "$recorded_image" > "$override"
compose=(docker compose -f ops/compose.private-pilot.yaml -f ops/compose.private-pilot-acceptance.yaml -f ops/compose.private-pilot-memory-acceptance.yaml -f ops/compose.private-pilot-combined-acceptance.yaml -f "$override")
if ! running_services="$("${compose[@]}" ps --status running --services)"; then
  echo "could not inspect B03E service state" >&2
  exit 1
fi
while IFS= read -r service; do
  case "$service" in
    "") ;;
    provider-worker) echo "provider worker must remain stopped" >&2; exit 1 ;;
    private-app) ;;
    *) echo "unknown running B03E service: $service" >&2; exit 1 ;;
  esac
done <<< "$running_services"
if ! owner_stop="$("${compose[@]}" run --rm --no-deps --pull never private-app python -B -c "from app.owner_control.store import OwnerStore; print(OwnerStore('/var/lib/pilot-app/owner').active())")" || [[ "$owner_stop" != True ]]; then
  echo "owner Stop must remain enabled" >&2
  exit 1
fi
"${compose[@]}" up -d --no-deps --no-build --pull never private-app
echo "Synthetic username: $username"
echo "Open $origin and sign in with the synthetic password used for the run."
