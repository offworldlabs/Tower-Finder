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
