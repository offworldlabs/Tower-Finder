#!/bin/bash
# ── Staging Smoke Tests ──────────────────────────────────────────────────────
# Run against the staging server to verify deployment health before
# promoting to production.
#
# Usage: bash deploy/staging-smoke-test.sh
# Exit code: 0 = all checks passed, 1 = failure
set -euo pipefail

BASE_URL="https://staging-towers.retina.fm"
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

check_rate_limit() {
    local name="$1" url="$2" tries="$3"
    printf "  %-40s " "$name"
    # A burst of requests must start getting 429s. Anything else means the
    # location is missing (the state staging was in before the nginx template
    # was shared with production) or that limit_req is keyed on something that
    # does not vary per client — behind Cloudflare, an unset `real_ip_header`
    # buckets per CF edge rather than per user and the limit never fires.
    #
    # The requests go out CONCURRENTLY, and must: a rate limit is only
    # observable while requests arrive faster than the zone refills, and
    # sequential curls cannot manage that against a per-second zone. At ~150 ms
    # per round trip from CI a serial loop sends ~7 r/s into `session`'s 5 r/s
    # refill, so draining its 21-token bucket would take ~79 requests — and at
    # >=200 ms latency it never drains at all, which is exactly how this check
    # failed against a correctly configured staging (run 31436629459: 30 serial
    # requests, all 200; 30 concurrent against the same host, 22/8).
    #
    # `tries` MUST exceed the zone's burst: `burst=N nodelay` admits N+1
    # requests before rejecting one, so a run of exactly N reports a false
    # failure.
    # `|| true` is load-bearing: xargs exits 123 if ANY child fails, and this
    # file runs under `set -euo pipefail`, so a bare assignment would abort the
    # whole script at this line — no FAIL line, no summary, and every later
    # check silently skipped. One flaky connection inside a 30-way concurrent
    # burst is precisely what this check provokes (ephemeral-port and TLS
    # handshake pressure on the runner), so tolerate partial failure and judge
    # on the codes that did come back, as the old serial loop's `|| continue`
    # did.
    local codes summary
    codes=$(seq 1 "$tries" | xargs -P "$tries" -I{} $CURL -o /dev/null -w '%{http_code}\n' "$url" 2>/dev/null || true)

    if printf '%s\n' "$codes" | grep -q '^429$'; then
        echo "OK (429 after burst)"
        PASS=$((PASS+1))
    else
        # Report the whole distribution: "all 200" means the limit never fired,
        # "all 000" means nothing was reachable, and a short count means the
        # burst partly failed — three different diagnoses that a single
        # last-code sample cannot tell apart.
        summary="${codes:+$(printf '%s\n' "$codes" | sort | uniq -c | awk '{printf "%s×%s ", $1, $2}')}"
        echo "FAIL (no 429 in $tries concurrent; got ${summary:-no responses})"
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
# Two zones, two checks. The credential surface carries the tight limit that
# actually resists brute force; the session reads a page load spends on every
# visit carry a looser one. Testing only /api/auth/me would leave the
# credential limit — the one that matters — unasserted.
check_rate_limit "credential endpoints rate limited" "${BASE_URL}/api/auth/login/google" 10
check_rate_limit "session endpoints rate limited"    "${BASE_URL}/api/auth/me"            30

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
