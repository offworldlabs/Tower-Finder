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
  # $1 = RETINA_ENV, $2 = NGINX_PROFILE, $3 = expected basename copied (or "" for none)
  local tmp; tmp="$(mktemp -d)"
  mkdir -p "${tmp}/app/deploy" "${tmp}/etc/nginx/sites-available"
  for p in staging local test; do echo "marker-${p}" > "${tmp}/app/deploy/nginx-${p}.conf"; done
  echo "marker-baked" > "${tmp}/etc/nginx/sites-available/default"
  RETINA_ENV="$1" NGINX_PROFILE="$2" APP_ROOT="${tmp}/app" NGINX_TARGET="${tmp}/etc/nginx/sites-available/default" \
    bash -c "${block}"
  local got; got="$(cat "${tmp}/etc/nginx/sites-available/default")"
  local want="marker-${3:-baked}"
  if [ "${got}" != "${want}" ]; then
    echo "FAIL: RETINA_ENV='$1' NGINX_PROFILE='$2' -> got '${got}', want '${want}'"; rm -rf "${tmp}"; exit 1
  fi
  rm -rf "${tmp}"
}

run_case "staging" ""      "staging"   # staging via RETINA_ENV default
run_case ""        "local" "local"     # laptop override
run_case "test"    ""      "test"      # test env defaults NGINX_PROFILE=RETINA_ENV
run_case ""        ""      ""           # prod: nothing selected, baked default kept
echo "PASS: nginx-profile selection"
