#!/bin/bash
# ── Staging Smoke Tests ──────────────────────────────────────────────────────
# Run against the staging server to verify deployment health before
# promoting to production.
#
# Usage: bash deploy/staging-smoke-test.sh
# Exit code: 0 = all checks passed, 1 = failure
set -euo pipefail

BASE_URL="https://staging.retina.fm"
API_URL="https://staging-api.retina.fm"
DASH_URL="https://staging-dash.retina.fm"
CURL="curl -s --connect-timeout 10 --max-time 30"
PASS=0
FAIL=0

check() {
    local name="$1" url="$2" expected="$3"
    printf "  %-40s " "$name"
    BODY=$($CURL "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    if echo "$BODY" | grep -q "$expected"; then
        echo "OK"
        PASS=$((PASS+1))
    else
        echo "FAIL (expected '$expected')"
        echo "    Response: $(echo "$BODY" | head -c 200)"
        FAIL=$((FAIL+1))
    fi
}

check_status() {
    local name="$1" url="$2" expected_code="$3"
    printf "  %-40s " "$name"
    CODE=$($CURL -o /dev/null -w "%{http_code}" "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    if [ "$CODE" = "$expected_code" ]; then
        echo "OK ($CODE)"
        PASS=$((PASS+1))
    else
        echo "FAIL (got $CODE, expected $expected_code)"
        FAIL=$((FAIL+1))
    fi
}

check_json_field() {
    local name="$1" url="$2" field="$3" min_value="$4"
    printf "  %-40s " "$name"
    BODY=$($CURL "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    VALUE=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin)$field)" 2>/dev/null) || {
        echo "FAIL (can't parse field $field)"
        FAIL=$((FAIL+1))
        return
    }

    if [ "$VALUE" -ge "$min_value" ] 2>/dev/null; then
        echo "OK ($VALUE >= $min_value)"
        PASS=$((PASS+1))
    else
        echo "FAIL ($VALUE < $min_value)"
        FAIL=$((FAIL+1))
    fi
}

check_header() {
    local name="$1" url="$2" header="$3"
    printf "  %-40s " "$name"
    HEADERS=$($CURL -o /dev/null -D - "$url" 2>/dev/null) || { echo "FAIL (connection error)"; FAIL=$((FAIL+1)); return; }

    if echo "$HEADERS" | tr 'A-Z' 'a-z' | grep -q "^${header}:"; then
        echo "OK"
        PASS=$((PASS+1))
    else
        echo "FAIL (no ${header} header)"
        FAIL=$((FAIL+1))
    fi
}

check_auth_rate_limit() {
    local name="$1" url="$2"
    printf "  %-40s " "$name"
    # The `auth` zone is 5r/m with burst=3, so a short run of requests must
    # start getting 429s. Anything else means the /api/auth/ location is
    # missing — which is exactly the state staging was in before the nginx
    # template was shared with production.
    local code saw_429=0
    for _ in $(seq 1 10); do
        code=$($CURL -o /dev/null -w "%{http_code}" "$url" 2>/dev/null) || continue
        if [ "$code" = "429" ]; then saw_429=1; break; fi
    done

    if [ "$saw_429" = "1" ]; then
        echo "OK (429 after burst)"
        PASS=$((PASS+1))
    else
        echo "FAIL (no 429 in 10 requests; last=$code)"
        FAIL=$((FAIL+1))
    fi
}

echo "═══════════════════════════════════════════════════"
echo "  Staging Smoke Tests"
echo "  frontend: ${BASE_URL}"
echo "  api:      ${API_URL}"
echo "  dash:     ${DASH_URL}"
echo "═══════════════════════════════════════════════════"

echo ""
echo "── Health & API endpoints (staging.retina.fm) ──"
check_status "GET /api/health"              "${BASE_URL}/api/health"        "200"
check_status "GET /api/radar/nodes"         "${BASE_URL}/api/radar/nodes"   "200"
check_status "GET /api/radar/analytics"     "${BASE_URL}/api/radar/analytics" "200"
check_status "GET /api/test/dashboard"      "${BASE_URL}/api/test/dashboard" "200"
check_status "GET /api/test/mlat-verification" "${BASE_URL}/api/test/mlat-verification" "200"
check_status "GET /api/config"              "${BASE_URL}/api/config"        "200"

echo ""
echo "── Dedicated API subdomain (staging-api.retina.fm) ──"
check_status "staging-api /api/health"      "${API_URL}/api/health"         "200"

echo ""
echo "── Dashboard subdomain (staging-dash.retina.fm) ──"
check_status "staging-dash GET /"           "${DASH_URL}/"                  "200"

echo ""
echo "── Frontend assets ──"
check_status "GET / (frontend)"             "${BASE_URL}/"                  "200"
check        "HTML has app root"            "${BASE_URL}/"                  "id=\"root\""

echo ""
echo "── Shared nginx config (must match production) ──"
# These used to exist only in production's hand-maintained nginx.conf, so a
# change that broke either of them reached prod untested. Both environments now
# render from deploy/nginx/nginx.conf.template — assert staging really serves
# them, so the shared config is exercised and not merely present in the repo.
#
# Deliberately probed on /api/ rather than on `/`: nginx drops inherited
# add_header directives in any location that declares its own, and the SPA's
# `location = /index.html` sets Cache-Control — so the HTML document itself
# carries none of these headers. That is long-standing production behaviour,
# preserved as-is by the template refactor and tracked separately; asserting it
# here on `/` would just fail.
check_header "CSP on dashboard vhost"       "${DASH_URL}/api/health" "content-security-policy"
check_header "CSP on frontend vhost"        "${BASE_URL}/api/health" "content-security-policy"
check_header "HSTS on api subdomain"        "${API_URL}/api/health"  "strict-transport-security"
check_auth_rate_limit "/api/auth/ is rate limited" "${BASE_URL}/api/auth/me"

echo ""
echo "── Detection archive (dash /data) ──"
# The Data Explorer reads this endpoint. It returns an empty list for the first
# hour after a deploy (ARCHIVE_FLUSH_INTERVAL_S), so assert the endpoint answers
# rather than that it has rows — the volume that makes those rows survive a
# rebuild is asserted by deploy/check-env-parity.sh instead.
check_status "GET /api/data/archive"        "${BASE_URL}/api/data/archive?limit=1" "200"

echo ""
echo "── Synthetic fleet data (wait for fleet to connect) ──"
# The fleet takes ~30-60s to fully connect; CI waits before calling this script
check_json_field "Active nodes > 0"         "${BASE_URL}/api/test/dashboard" "['nodes']['active']" "1"

echo ""
echo "═══════════════════════════════════════════════════"
echo "  Results: ${PASS} passed, ${FAIL} failed"
echo "═══════════════════════════════════════════════════"

if [ "$FAIL" -gt 0 ]; then
    echo "STAGING SMOKE TESTS FAILED"
    exit 1
fi
echo "ALL STAGING SMOKE TESTS PASSED"
exit 0
