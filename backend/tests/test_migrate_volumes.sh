#!/usr/bin/env bash
# Seeds a volume with content and a non-root owner, migrates it, and asserts the
# copy is faithful. Ownership is the part that matters: backend-data is owned by
# the application's real uid, 999 (Dockerfile:81, `useradd -r appuser`), and a
# root-flattened copy would break the app.
#
# The negative cases below prove the verification logic itself rejects a bad
# copy, not just that a good copy happens to pass: a comparison that always
# reported success would still pass a suite made only of the happy path.
#
# Not wired into CI or pre-commit (it needs a working Docker daemon); run it
# by hand with `bash backend/tests/test_migrate_volumes.sh`, and run it
# before any real migration to confirm the verification logic on this box.
set -euo pipefail

SCRIPT="$(cd "$(dirname "$0")/../.." && pwd)/deploy/migrate-volumes.sh"
OLD="tfmigtest-old"
NEW="tfmigtest-new"
VOL="backend-data"
SRC_VOL="${OLD}_${VOL}"
DST_VOL="${NEW}_${VOL}"

cleanup() { docker volume rm -f "$SRC_VOL" "$DST_VOL" >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

seed_source() {
    docker volume create "$SRC_VOL" >/dev/null
    docker run --rm -v "$SRC_VOL":/v alpine sh -c '
        mkdir -p /v/runtime
        echo hello > /v/state_snapshot.json
        echo world > /v/runtime/tower_config.json
        chown -R 999:999 /v'
}

### Happy path ###

seed_source
bash "$SCRIPT" "$OLD" "$NEW" "$VOL"

docker run --rm -v "$DST_VOL":/v alpine sh -c '
  set -e
  [ "$(cat /v/state_snapshot.json)" = "hello" ] || { echo "FAIL: content"; exit 1; }
  [ "$(cat /v/runtime/tower_config.json)" = "world" ] || { echo "FAIL: nested content"; exit 1; }
  [ "$(stat -c %u /v/state_snapshot.json)" = "999" ] || { echo "FAIL: uid not preserved"; exit 1; }
  echo PASS'

### Negative cases ###
#
# Each corrupts the destination that the happy path above already produced,
# then checks it with verify_volume directly rather than re-running the whole
# script: a full re-run recreates the destination with a fresh cp -a, which
# would silently heal the corruption before any check saw it.

assert_verify_fails() {
    local case_name="$1" out status
    set +e
    out="$(bash -c 'source "$1"; verify_volume "$2" "$3" "$4"' _ \
        "$SCRIPT" "$SRC_VOL" "$DST_VOL" "$VOL" 2>&1)"
    status=$?
    set -e
    if [ "$status" -eq 0 ]; then
        echo "FAIL: $case_name: verify_volume exited 0, expected non-zero"
        echo "$out"
        exit 1
    fi
    case "$out" in
        *"$VOL"*) ;;
        *)
            echo "FAIL: $case_name: message did not name the volume ($VOL)"
            echo "$out"
            exit 1
            ;;
    esac
    echo "PASS: $case_name (exit $status): $out"
}

# Re-seed a known-good pair before each corruption so the cases are
# independent and a failure in one cannot corrupt the next.
reset_pair() {
    docker volume rm -f "$SRC_VOL" "$DST_VOL" >/dev/null 2>&1 || true
    seed_source
    bash "$SCRIPT" "$OLD" "$NEW" "$VOL" >/dev/null
}

reset_pair
docker run --rm -v "$DST_VOL":/v alpine sh -c 'truncate -s 2 /v/state_snapshot.json'
assert_verify_fails "truncated file in destination"

reset_pair
docker run --rm -v "$DST_VOL":/v alpine sh -c 'rm /v/runtime/tower_config.json'
assert_verify_fails "dropped file in destination"

reset_pair
docker run --rm -v "$DST_VOL":/v alpine sh -c '
  # "goodb\n" is 6 bytes, same as the seeded "hello\n": the cheap count/byte
  # gate must pass, so only the content manifest can catch this one.
  printf "goodb\n" > /v/state_snapshot.json'
assert_verify_fails "same-size content change in destination"

echo "ALL PASS"
