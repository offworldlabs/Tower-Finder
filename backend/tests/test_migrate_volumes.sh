#!/usr/bin/env bash
# Seeds a volume with content and a non-root owner, migrates it, and asserts the
# copy is faithful. Ownership is the part that matters: backend-data is owned by
# the application's uid, and a root-flattened copy would break the app.
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/../.." && pwd)/deploy/migrate-volumes.sh"
OLD="tfmigtest-old"
NEW="tfmigtest-new"
VOL="backend-data"

cleanup() { docker volume rm -f "${OLD}_${VOL}" "${NEW}_${VOL}" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

docker volume create "${OLD}_${VOL}" >/dev/null
docker run --rm -v "${OLD}_${VOL}":/v alpine sh -c '
  mkdir -p /v/runtime
  echo hello > /v/state_snapshot.json
  echo world > /v/runtime/tower_config.json
  chown -R 1001:1001 /v'

bash "$SCRIPT" "$OLD" "$NEW" "$VOL"

docker run --rm -v "${NEW}_${VOL}":/v alpine sh -c '
  set -e
  [ "$(cat /v/state_snapshot.json)" = "hello" ] || { echo "FAIL: content"; exit 1; }
  [ "$(cat /v/runtime/tower_config.json)" = "world" ] || { echo "FAIL: nested content"; exit 1; }
  [ "$(stat -c %u /v/state_snapshot.json)" = "1001" ] || { echo "FAIL: uid not preserved"; exit 1; }
  echo PASS'
