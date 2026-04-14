#!/bin/bash
# Add Cloudflare DNS A records for staging subdomains.
# Run once with a valid CF_TOKEN env var:
#   CF_TOKEN=<your-token> bash deploy/add-staging-dns.sh
#
# Token needs "Edit zone DNS" permission for retina.fm.
# Create at: dash.cloudflare.com → My Profile → API Tokens → Create Token

set -euo pipefail

: "${CF_TOKEN:?CF_TOKEN is required}"

STAGING_IP="174.138.70.197"
ZONE_NAME="retina.fm"

echo "Fetching zone ID for ${ZONE_NAME}..."
ZONE_ID=$(curl -s -X GET "https://api.cloudflare.com/client/v4/zones?name=${ZONE_NAME}" \
  -H "Authorization: Bearer ${CF_TOKEN}" \
  -H "Content-Type: application/json" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result'][0]['id'])")

echo "Zone ID: ${ZONE_ID}"

add_record() {
  local name="$1"
  echo "Adding A record: ${name} → ${STAGING_IP} (proxied=true)..."
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records" \
    -H "Authorization: Bearer ${CF_TOKEN}" \
    -H "Content-Type: application/json" \
    --data "{
      \"type\": \"A\",
      \"name\": \"${name}\",
      \"content\": \"${STAGING_IP}\",
      \"ttl\": 1,
      \"proxied\": true
    }" | python3 -c "
import sys, json
d = json.load(sys.stdin)
if d['success']:
    r = d['result']
    print(f'  ✓ {r[\"name\"]} → {r[\"content\"]} (id={r[\"id\"]})')
else:
    print(f'  ✗ FAILED: {d[\"errors\"]}')
    sys.exit(1)
"
}

add_record "staging"
add_record "staging-api"
add_record "staging-dash"

echo ""
echo "Done. DNS records propagate through Cloudflare within seconds."
echo "Domains: staging.retina.fm, staging-api.retina.fm, staging-dash.retina.fm"
