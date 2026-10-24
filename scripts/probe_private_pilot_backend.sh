#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 || $# -gt 3 ]]; then
  echo "usage: $0 SUBNET BACKEND_IP [APP_IMAGE]" >&2
  exit 2
fi

probe_subnet=$1
probe_ip=$2
probe_image=${3:-almost-reasonable-private-pilot-app:local}
probe_name="almost-reasonable-acceptance-probe-$$"

python3 - "$probe_subnet" "$probe_ip" <<'PY'
import os
import sys
from acceptance.private_pilot_live import _backend_network, _check_backend_conflicts

subnet, _ = _backend_network(sys.argv[1], sys.argv[2])
_check_backend_conflicts(subnet, os.environ.copy())
PY

cleanup() {
  docker rm -f "$probe_name" >/dev/null 2>&1 || true
  docker network rm "$probe_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create --internal --subnet "$probe_subnet" "$probe_name" >/dev/null
docker run -d --read-only --name "$probe_name" \
  --network "$probe_name" --ip "$probe_ip" \
  "$probe_image" python -B -m http.server 8000 --bind 0.0.0.0 >/dev/null

probe_inside_network() {
  docker run --rm --read-only --network "$probe_name" "$probe_image" \
    python -B -c 'import sys, urllib.request; response=urllib.request.urlopen("http://"+sys.argv[1]+":8000/", timeout=2); assert response.status == 200' \
    "$probe_ip" >/dev/null 2>&1
}

for attempt in {1..30}; do
  if probe_inside_network; then
    break
  fi
  if [[ $attempt -eq 30 ]]; then
    echo "internal-network HTTP probe failed at $probe_ip:8000" >&2
    exit 1
  fi
  sleep 0.2
done
echo "internal-network HTTP probe passed at $probe_ip:8000"

for attempt in {1..30}; do
  if curl --fail --silent --show-error --max-time 2 \
    "http://$probe_ip:8000/" >/dev/null 2>&1; then
    break
  fi
  if [[ $attempt -eq 30 ]]; then
    echo "host cannot reach the internal-network backend at $probe_ip:8000" >&2
    exit 1
  fi
  sleep 0.2
done

docker restart "$probe_name" >/dev/null
actual_ip=$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$probe_name")
if [[ $actual_ip != "$probe_ip" ]]; then
  echo "backend IP changed after restart: $actual_ip" >&2
  exit 1
fi
for attempt in {1..30}; do
  if probe_inside_network; then
    break
  fi
  if [[ $attempt -eq 30 ]]; then
    echo "internal-network HTTP probe failed after restart" >&2
    exit 1
  fi
  sleep 0.2
done
for attempt in {1..30}; do
  if curl --fail --silent --show-error --max-time 2 \
    "http://$probe_ip:8000/" >/dev/null 2>&1; then
    echo "backend reachable at $probe_ip:8000 before and after restart"
    exit 0
  fi
  sleep 0.2
done
echo "backend unreachable after restart" >&2
exit 1
