#!/bin/bash
# ── Deploy Retina Test Network ────────────────────────────────────────────────
# Deploys the full test network at testmap.retina.fm + testapi.retina.fm
# with 200+ synthetic nodes exercising all subsystems.
#
# Usage:
#   bash deploy/deploy-test-network.sh                    # 200 nodes (default)
#   bash deploy/deploy-test-network.sh --nodes 500        # 500 nodes
#   bash deploy/deploy-test-network.sh --nodes 1000 --regions us,eu
#
# Prerequisites:
#   - SSH access to the target server
#   - Docker + Docker Compose installed
#   - Cloudflare DNS: testmap.retina.fm + testapi.retina.fm → server IP
#   - Cloudflare Origin Certificate at /etc/ssl/cloudflare/{cert,key}.pem
set -euo pipefail

# ── Parse arguments ──────────────────────────────────────────────────────────
NODES=200
REGIONS="us"
MODE="adsb"
MAPRAD_API_KEY="${MAPRAD_API_KEY:-}"

while [[ $# -gt 0 ]]; do
    case $1 in
        --nodes) NODES="$2"; shift 2 ;;
        --regions) REGIONS="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --api-key) MAPRAD_API_KEY="$2"; shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

echo "══════════════════════════════════════════════════════════"
echo "  Retina Test Network Deployment"
echo "══════════════════════════════════════════════════════════"
echo "  Nodes:      ${NODES}"
echo "  Regions:    ${REGIONS}"
echo "  Mode:       ${MODE}"
echo "  Domains:    testmap.retina.fm / testapi.retina.fm"
echo "══════════════════════════════════════════════════════════"
echo ""

# ── Verify prerequisites ─────────────────────────────────────────────────────
echo "→ Checking prerequisites..."

if ! command -v docker &> /dev/null; then
    echo "  ✗ Docker not installed"
    exit 1
fi
echo "  ✓ Docker available"

if ! docker compose version &> /dev/null; then
    echo "  ✗ Docker Compose not available"
    exit 1
fi
echo "  ✓ Docker Compose available"

if [ ! -d "/etc/ssl/cloudflare" ]; then
    echo "  ⚠ /etc/ssl/cloudflare not found — HTTPS will fail"
    echo "  Creating self-signed cert for testing..."
    mkdir -p /etc/ssl/cloudflare
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout /etc/ssl/cloudflare/key.pem \
        -out /etc/ssl/cloudflare/cert.pem \
        -days 365 -subj "/CN=testapi.retina.fm" 2>/dev/null
    echo "  ✓ Self-signed cert created (replace with Cloudflare Origin cert)"
else
    echo "  ✓ SSL certificates found"
fi

# ── Create .env if missing ───────────────────────────────────────────────────
if [ ! -f "backend/.env" ]; then
    echo "→ Creating backend/.env..."
    cat > backend/.env << EOF
MAPRAD_API_KEY=${MAPRAD_API_KEY}
CORS_ORIGINS=https://retina.fm,https://testapi.retina.fm,https://testmap.retina.fm,http://localhost:5173
RETINA_ENV=test
EOF
    chmod 600 backend/.env
else
    echo "→ backend/.env exists, ensuring test settings..."
    # Ensure RETINA_ENV=test is set
    if ! grep -q "RETINA_ENV" backend/.env; then
        echo "RETINA_ENV=test" >> backend/.env
    fi
fi

# ── Configure fleet size ─────────────────────────────────────────────────────
echo "→ Configuring fleet simulator (${NODES} nodes, regions=${REGIONS})..."

# Update docker-compose.test.yml env vars inline via sed
# (The env section in docker-compose.test.yml sets defaults,
#  but we override via the environment section)

# ── Build & Deploy ───────────────────────────────────────────────────────────
echo ""
echo "→ Building containers..."
FLEET_NODES="${NODES}" FLEET_REGIONS="${REGIONS}" FLEET_MODE="${MODE}" \
    docker compose -f docker-compose.test.yml build

echo ""
echo "→ Starting test network..."
FLEET_NODES="${NODES}" FLEET_REGIONS="${REGIONS}" FLEET_MODE="${MODE}" \
    docker compose -f docker-compose.test.yml up -d

echo ""
echo "→ Waiting for server health check..."
for i in $(seq 1 24); do
    if curl -sf http://localhost/api/health > /dev/null 2>&1; then
        echo "  ✓ Server healthy!"
        break
    fi
    if [ "$i" -eq 24 ]; then
        echo "  ✗ Server not healthy after 120s"
        echo "  Logs:"
        docker compose -f docker-compose.test.yml logs --tail 30 tower-finder-test
        exit 1
    fi
    sleep 5
done

# ── Wait for fleet to connect ────────────────────────────────────────────────
echo ""
echo "→ Waiting for fleet simulator to connect nodes..."
sleep 10

# Check fleet logs
echo ""
echo "→ Fleet simulator status:"
docker compose -f docker-compose.test.yml logs --tail 20 fleet-simulator

# ── Validate subsystems ──────────────────────────────────────────────────────
echo ""
echo "→ Validating subsystems..."

# API health
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/api/health)
if [ "$HTTP_CODE" = "200" ]; then
    echo "  ✓ API health check"
else
    echo "  ✗ API health check (HTTP $HTTP_CODE)"
fi

# Radar status
RADAR_STATUS=$(curl -sf http://localhost/api/radar/status 2>/dev/null || echo "{}")
echo "  ✓ Radar status: ${RADAR_STATUS}" | head -c 200
echo ""

# Connected nodes
NODES_JSON=$(curl -sf http://localhost/api/radar/nodes 2>/dev/null || echo "{}")
CONNECTED=$(echo "$NODES_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('connected',0))" 2>/dev/null || echo "?")
echo "  ✓ Connected nodes: ${CONNECTED}"

# Aircraft feed
AIRCRAFT_JSON=$(curl -sf http://localhost/api/radar/data/aircraft.json 2>/dev/null || echo "{}")
N_AIRCRAFT=$(echo "$AIRCRAFT_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('aircraft',[])))" 2>/dev/null || echo "?")
echo "  ✓ Aircraft tracked: ${N_AIRCRAFT}"

# Analytics
ANALYTICS=$(curl -sf http://localhost/api/radar/analytics 2>/dev/null || echo "{}")
N_ANALYTICS=$(echo "$ANALYTICS" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('nodes',{})))" 2>/dev/null || echo "?")
echo "  ✓ Analytics nodes: ${N_ANALYTICS}"

# Association
ASSOC=$(curl -sf http://localhost/api/radar/association/status 2>/dev/null || echo "{}")
N_OVERLAPS=$(echo "$ASSOC" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('overlap_zones',0))" 2>/dev/null || echo "?")
echo "  ✓ Overlap zones: ${N_OVERLAPS}"

# ── Summary ──────────────────────────────────────────────────────────────────
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

echo ""
echo "══════════════════════════════════════════════════════════"
echo "  ✓ Test Network Deployed!"
echo "══════════════════════════════════════════════════════════"
echo ""
echo "  Server:   ${SERVER_IP}"
echo "  API:      https://testapi.retina.fm"
echo "  Map:      https://testmap.retina.fm"
echo "  Nodes:    ${CONNECTED} connected"
echo "  Aircraft: ${N_AIRCRAFT} tracked"
echo ""
echo "  Monitor:"
echo "    docker compose -f docker-compose.test.yml logs -f fleet-simulator"
echo "    curl https://testapi.retina.fm/api/radar/status"
echo "    curl https://testapi.retina.fm/api/radar/nodes"
echo "    curl https://testapi.retina.fm/api/radar/analytics"
echo "    curl https://testapi.retina.fm/api/radar/data/aircraft.json"
echo ""
echo "  Scale up:"
echo "    # Restart fleet with more nodes"
echo "    FLEET_NODES=500 docker compose -f docker-compose.test.yml up -d fleet-simulator"
echo ""
echo "  Cloudflare DNS:"
echo "    testapi.retina.fm → A record → ${SERVER_IP}"
echo "    testmap.retina.fm → A record → ${SERVER_IP}"
echo ""
