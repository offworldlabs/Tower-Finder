#!/usr/bin/env bash
# Unit test for start.sh's nginx-profile selection. Extracts the selection
# block (delimited by the two markers below) and runs it against a temp dir of
# stub nginx-<profile>.conf files, asserting the correct file is copied in.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
start_sh="${here}/../start.sh"

# Pull the block between the markers out of start.sh so the test tracks the
# real code rather than a copy.
block="$(awk '/# >>> nginx-profile-select >>>/{f=1;next} /# <<< nginx-profile-select <<</{f=0} f' "${start_sh}")"
[ -n "${block}" ] || { echo "FAIL: could not extract nginx-profile-select block from start.sh"; exit 1; }

run_case() {
  # $1 = RETINA_ENV, $2 = NGINX_PROFILE
  # $3 = expected basename copied ("" for none/baked), or "FAIL" for a
  #      fail-closed case: block must exit non-zero and leave the baked
  #      default untouched.
  local tmp; tmp="$(mktemp -d)"
  mkdir -p "${tmp}/app/deploy" "${tmp}/etc/nginx/sites-available"
  # Stub the configs that ship in the repo. A profile with no matching stub
  # (e.g. "bogus" below) must hit the fail-closed path rather than find one.
  for p in staging local; do echo "marker-${p}" > "${tmp}/app/deploy/nginx-${p}.conf"; done
  echo "marker-baked" > "${tmp}/etc/nginx/sites-available/default"

  # `bash -c` already runs the block in a child process, so its `exit 1`
  # cannot terminate this script directly. But this script itself runs under
  # `set -e`, so the failing command still needs the `|| rc=$?` guard here,
  # otherwise set -e would abort the test script before we get to assert on
  # the failure we expected.
  local rc=0
  RETINA_ENV="$1" NGINX_PROFILE="$2" APP_ROOT="${tmp}/app" NGINX_TARGET="${tmp}/etc/nginx/sites-available/default" \
    bash -c "${block}" || rc=$?
  local got; got="$(cat "${tmp}/etc/nginx/sites-available/default")"

  if [ "$3" = "FAIL" ]; then
    if [ "${rc}" -eq 0 ]; then
      echo "FAIL: RETINA_ENV='$1' NGINX_PROFILE='$2' -> expected non-zero exit (fail closed), got 0"; rm -rf "${tmp}"; exit 1
    fi
    if [ "${got}" != "marker-baked" ]; then
      echo "FAIL: RETINA_ENV='$1' NGINX_PROFILE='$2' -> target became '${got}'; fail-closed case must leave the baked default untouched"; rm -rf "${tmp}"; exit 1
    fi
  else
    if [ "${rc}" -ne 0 ]; then
      echo "FAIL: RETINA_ENV='$1' NGINX_PROFILE='$2' -> unexpected non-zero exit ${rc}"; rm -rf "${tmp}"; exit 1
    fi
    local want="marker-${3:-baked}"
    if [ "${got}" != "${want}" ]; then
      echo "FAIL: RETINA_ENV='$1' NGINX_PROFILE='$2' -> got '${got}', want '${want}'"; rm -rf "${tmp}"; exit 1
    fi
  fi
  rm -rf "${tmp}"
}

run_case "staging" "staging" "staging"  # staging: explicit profile
run_case "test"    "local"   "local"    # laptop/test stack: explicit local profile
run_case "test"    ""        ""         # PROD SHAPE: RETINA_ENV=test but no profile -> baked prod config kept (nginx is NOT derived from RETINA_ENV)
run_case ""        ""        ""          # prod: nothing set, baked default kept
run_case "test"    "staging" "staging"  # profile is independent of RETINA_ENV
run_case ""        "bogus"   "FAIL"      # fail closed: named profile with no matching config, exit 1, baked default untouched
echo "PASS: nginx-profile selection"
