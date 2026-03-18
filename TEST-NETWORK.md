# Retina Test Network — Deployment & Operations Guide

## Overview

The test network deploys a full-scale Retina passive radar system at:
- **testapi.retina.fm** — API, WebSocket, TCP node protocol
- **testmap.retina.fm** — Live tar1090 aircraft map

It runs **100-1000 synthetic nodes** generating realistic detection data from
a shared SimulationWorld, exercising all subsystems end-to-end:

| Subsystem | What it proves |
|-----------|---------------|
| TCP protocol | HELLO→CONFIG→DETECTION→HEARTBEAT at scale |
| Passive radar pipeline | Tracker + solver process 1000s of frames/sec |
| Multi-node solver | Cross-node association + LM geolocation |
| Node analytics | Trust scoring, reputation, SNR stats, uptime |
| Data archival | Detection frames archived locally (+ B2 optional) |
| Aircraft feed | aircraft.json + WebSocket broadcast to map clients |
| Live map | tar1090 displays tracked aircraft in real-time |

## Quick Start (Local)

```bash
# 1. Build and launch (200 nodes by default)
docker compose -f docker-compose.test.yml up -d --build

# 2. Watch fleet simulator
docker compose -f docker-compose.test.yml logs -f fleet-simulator

# 3. Check dashboard
curl http://localhost/api/test/dashboard | python3 -m json.tool

# 4. Open map in browser
open http://localhost  # → tar1090 aircraft map
```

## Deploy to Server

```bash
# On the server (Ubuntu 24.04 + Docker):
bash deploy/deploy-test-network.sh --nodes 200

# Scale up to 500 nodes:
bash deploy/deploy-test-network.sh --nodes 500 --regions us,eu

# Scale to 1000:
bash deploy/deploy-test-network.sh --nodes 1000 --regions us,eu,au
```

### DNS Setup (Cloudflare)

Create A records pointing to your server IP:
```
testapi.retina.fm → <SERVER_IP>  (Proxied)
testmap.retina.fm → <SERVER_IP>  (Proxied)
```

Create an Origin Certificate in Cloudflare SSL/TLS → Origin Server,
then place files on the server:
```
/etc/ssl/cloudflare/cert.pem
/etc/ssl/cloudflare/key.pem
```

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  docker-compose.test.yml                                │
│                                                         │
│  ┌──────────────────────┐   ┌────────────────────────┐ │
│  │  tower-finder-test   │   │  fleet-simulator       │ │
│  │                      │   │                        │ │
│  │  nginx (80/443)      │   │  SimulationWorld       │ │
│  │    ↕                 │   │  (shared aircraft)     │ │
│  │  FastAPI (8000)      │←TCP│                        │ │
│  │    ↕                 │3012│  200-1000 NodeConns    │ │
│  │  TCP Server (3012)   │   │  (async TCP clients)   │ │
│  │    ↕                 │   │                        │ │
│  │  Radar Pipeline      │   │  Ground Truth Logger   │ │
│  │  Node Analytics      │   │  Validation Loop       │ │
│  │  Association Engine   │   │                        │ │
│  │  Data Archival       │   └────────────────────────┘ │
│  │  WebSocket Broadcast │                               │
│  └──────────────────────┘                               │
└─────────────────────────────────────────────────────────┘
         │                              │
         ↓                              ↓
  testapi.retina.fm              ground_truth.json
  testmap.retina.fm
```

## Monitoring Endpoints

### Dashboard — All subsystems at a glance
```bash
curl https://testapi.retina.fm/api/test/dashboard
```

### Connected nodes
```bash
curl https://testapi.retina.fm/api/radar/nodes
```

### Per-node analytics (trust, SNR, uptime)
```bash
curl https://testapi.retina.fm/api/radar/analytics
curl https://testapi.retina.fm/api/radar/analytics/synth-US-0001
```

### Live aircraft feed
```bash
curl https://testapi.retina.fm/api/radar/data/aircraft.json
```

### Multi-node association status
```bash
curl https://testapi.retina.fm/api/radar/association/status
```

### Data archive
```bash
curl https://testapi.retina.fm/api/data/archive
```

### Validation (compare solver output against ground truth)
```bash
curl -X POST https://testapi.retina.fm/api/test/validate \
  -H "Content-Type: application/json" \
  -d '{"ground_truth": [{"id":"obj-001","lat":33.5,"lon":-84.3,"alt_km":10}]}'
```

## Configuration

### Fleet size (docker-compose.test.yml environment)

| Variable | Default | Description |
|----------|---------|-------------|
| `FLEET_NODES` | 200 | Number of synthetic nodes |
| `FLEET_REGIONS` | us | Regions: us, eu, au (comma-separated) |
| `FLEET_MODE` | adsb | detection, adsb, or anomalous |
| `FLEET_INTERVAL` | 0.5 | Frame interval in seconds |
| `FLEET_VALIDATE` | true | Enable validation loop |

### Scaling to fleet size

| Scale | CPU | RAM | Notes |
|-------|-----|-----|-------|
| 100 nodes | 2 CPU | 2 GB | Single server, comfortable |
| 200 nodes | 4 CPU | 4 GB | Default test network |
| 500 nodes | 4 CPU | 6 GB | May need uvicorn workers=2 |
| 1000 nodes | 8 CPU | 8 GB | Large droplet recommended |

Scale the fleet live:
```bash
# Restart fleet with new size
docker compose -f docker-compose.test.yml stop fleet-simulator
FLEET_NODES=500 docker compose -f docker-compose.test.yml up -d fleet-simulator
```

## Fleet Generator

Generate node configurations independently:
```bash
cd backend

# 200 US nodes
python fleet_generator.py --nodes 200 --regions us --output fleet_config.json

# 500 nodes across US + Europe
python fleet_generator.py --nodes 500 --regions us,eu --output fleet_config.json

# 1000 nodes globally
python fleet_generator.py --nodes 1000 --regions us,eu,au --output fleet_config.json
```

## Fleet Orchestrator

Run without Docker (for development):
```bash
cd backend

# Start server first
uvicorn main:app --host 0.0.0.0 --port 8000 &

# Run fleet (200 nodes, local server)
python fleet_orchestrator.py --config fleet_config.json --host localhost --port 3012

# Run with validation
python fleet_orchestrator.py --config fleet_config.json --validate --validation-url http://localhost:8000

# 60-second test run
python fleet_orchestrator.py --config fleet_config.json --duration 60
```

## Validation

The fleet orchestrator records **ground truth** from the SimulationWorld — the
actual positions of all simulated aircraft at each timestep. This can be
compared against the server's solved positions to validate:

1. **Detection rate**: What % of simulated aircraft appear on the map?
2. **Position accuracy**: How far are solved positions from ground truth?
3. **Altitude accuracy**: How close is the altitude estimate?
4. **False track rate**: How many server tracks have no ground truth match?

The validation loop runs automatically when `FLEET_VALIDATE=true`, logging
results every 30 seconds. Ground truth is also saved to `ground_truth.json`
on shutdown.

## What This Proves

1. **Ready for real users** — All subsystems handle 100-1000 concurrent
   nodes with realistic detection data flowing through the full pipeline.

2. **Software works** — Live map at testmap.retina.fm shows tracked aircraft
   that can be validated against known simulation ground truth.

3. **Scalable architecture** — The system handles the load with measurable
   performance metrics (frames/sec, detection rate, solver accuracy).

4. **Data integrity** — Detection data is archived, analytics are computed,
   trust scores are evaluated, and the full API surface is exercised.
