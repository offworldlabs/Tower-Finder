# Tower Finder / RETINA

This repo powers RETINA, a passive-radar system: receiver nodes detect aircraft
from reflections of broadcast transmitters, and the backend turns those
detections into live tracks shown on a web map. "Tower Finder" — the original
illuminator-search feature — is one of several surfaces served by the same
FastAPI app.

> **New here?** Read [`ONBOARDING.md`](ONBOARDING.md) for full local setup, tests
> and how code ships, and [`docs/architecture.md`](docs/architecture.md) for how
> the pieces fit together. This README is the short version.

## Surfaces

One backend serves several React front-ends, distinguished by subdomain:

| Surface | What it is |
| --- | --- |
| **testmap** | Live aircraft map fed by the simulation fleet (synthetic nodes) — the main dev/demo surface. |
| **map** | Production live map showing only real radar nodes. |
| **dashboard** | Admin app (auth required): node ownership, claim codes, MLAT verification, metrics. |
| **Tower Finder** | The original `/api/towers` illuminator search. |

Receiver nodes connect over TCP (port 3012) and stream detection frames; the
pipeline turns those into aircraft positions broadcast to the map over WebSocket.

## Repo layout

```
backend/          FastAPI API, TCP frame ingest, detection pipeline, background tasks
frontend/         React SPA — testmap / map / tower search (Vite + Leaflet)
dashboard/        React admin app (Vite)
deploy/           nginx configs, start/deploy/rollback scripts
docs/             Architecture, pipeline, runbook, alerting, simulation, arc-display
libs/             Git submodules (algorithm libraries, versioned independently)
  retina-geolocator/   Bistatic delay/Doppler position solver (single- and multi-node)
  retina-tracker/      Multi-target Kalman tracker with anomaly detection
  retina-simulation/   Fleet simulator generating synthetic frames for testmap/CI
  retina-custody/      Custody-protocol library
  retina-analytics/    Node trust/reputation analysis
```

## Quick start

### Clone (with submodules)

```bash
git clone --recursive https://github.com/offworldlabs/Tower-Finder.git
cd Tower-Finder

# If already cloned without --recursive:
git submodule update --init --recursive
```

### Backend

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -e ../libs/retina-geolocator -e ../libs/retina-tracker \
            -e ../libs/retina-custody -e ../libs/retina-simulation \
            -e ../libs/retina-analytics
cp .env.example .env    # add your Maprad.io API key (needed for AU/CA searches)
RETINA_ENV=dev uvicorn main:app --reload
```

The API runs at `http://localhost:8000`. Interactive docs at `/docs`.

`RETINA_ENV=dev` enables an auth bypass so you don't need OAuth credentials
locally — you're treated as an anonymous admin.

### Frontend

```bash
cd frontend    # or: cd dashboard
npm install
npm run dev
```

Opens at `http://localhost:5173`; `/api` and `/ws` are proxied to the backend on
`:8000`. The hostname selects the surface — `localhost` shows tower search, and
`http://testmap.localhost:5173/` gets you the live map.

Running the tests, the simulation fleet and the deploy pipeline are all covered
in [`ONBOARDING.md`](ONBOARDING.md).

## Tower Finder API

### `GET /api/towers`

| Parameter     | Type   | Required | Default | Description                                                        |
|---------------|--------|----------|---------|--------------------------------------------------------------------|
| `lat`         | float  | yes      |         | Latitude (−90 to 90)                                               |
| `lon`         | float  | yes      |         | Longitude (−180 to 180)                                            |
| `altitude`    | float  | no       | 0       | Receiver altitude in metres. `0` looks up ground elevation instead |
| `radius_km`   | int    | no       | 0       | Search radius, 0–300. `0` uses the configured default (80)         |
| `limit`       | int    | no       | 0       | Max towers to return, 0–200. `0` uses the configured default (100) |
| `source`      | string | no       | `auto`  | Data source: `auto`, `us`, `au`, `ca`. `auto` detects from `lat`/`lon` |
| `frequencies` | string | no       |         | Comma-separated measured frequencies in MHz (max 10). Towers within ±5 MHz are flagged and ranked first |

**Response:**

```json
{
  "towers": [
    {
      "rank": 1,
      "callsign": "ATN6",
      "name": "ABC Tower 221 Pacific Highway GORE HILL",
      "state": "NSW",
      "frequency_mhz": 177.5,
      "band": "VHF",
      "latitude": -33.820079,
      "longitude": 151.185,
      "antenna_height_m": 180,
      "distance_km": 12.4,
      "bearing_deg": 337.5,
      "bearing_cardinal": "NNW",
      "received_power_dbm": -21.3,
      "distance_class": "Ideal",
      "eirp_dbm": 79.1,
      "licence_type": "Broadcasting",
      "licence_subtype": "Commercial Television",
      "frequency_matched": false,
      "elevation_m": 128.0,
      "altitude_m": 308.0
    }
  ],
  "query": {
    "latitude": -33.8688,
    "longitude": 151.2093,
    "altitude_m": 24.0,
    "radius_km": 80,
    "source": "au",
    "user_frequencies_mhz": []
  },
  "count": 100
}
```

`elevation_m` (ground height at the tower) and `altitude_m` (ground + antenna
height) come from the Open-Meteo elevation API and are `null` if that lookup
fails.

Also on this surface: `GET /api/elevation` for a single point, `GET /api/health`
for liveness (`?strict=1` for readiness), and `GET`/`PUT /api/config` to read and
update the ranking config (the `PUT` requires admin). The full API — radar
ingest, WebSocket aircraft feeds, archive, admin — is browsable at `/docs`.

## How ranking works

1. Fetch broadcast transmitters within the search radius (default 80 km) from the
   source for that region.
2. Keep only transmitters in a configured broadcast band: FM 87.8–108, VHF
   174–216, UHF 470–608 MHz.
3. Estimate EIRP from the record's power data, falling back to 50 dBm (FM) or
   60 dBm otherwise when the record has none, then compute received power via
   free-space path loss plus the configured receive-antenna gain (6 dBi).
4. Discard anything below the receiver sensitivity floor (−120 dBm).
5. Classify each tower by distance suitability:
   - **Too Close** (< 8 km) — direct signal may overwhelm the receiver
   - **Ideal** (8–30 km) — best bistatic geometry
   - **Good** (30–60 km) — workable
   - **Far** (> 60 km) — fallback only
6. Deduplicate by callsign + frequency, keeping the strongest.
7. Rank by: frequency match (if `frequencies` was supplied) → band preference
   (VHF → UHF → FM) → distance class (Ideal → Good → Far → Too Close) → signal
   strength.
8. Return the top N results.

Every threshold above — bands, distance classes, sensitivity, antenna gain, sort
order, defaults — lives in `backend/config/tower_config.json` and is editable at
runtime through `PUT /api/config` or the dashboard. The values quoted here are
the shipped defaults.

## Tech stack

- **Backend:** Python 3.12, FastAPI, httpx, numpy/scipy (position solver),
  fastapi-users + SQLite (auth, node ownership), pyarrow (Parquet archive),
  boto3 (Cloudflare R2 offload)
- **Frontend:** React 18, TypeScript, Vite, Leaflet; Vitest + Playwright for tests
- **Dashboard:** the same, plus React Router and Recharts
- **Deployment:** Docker (multi-stage build, nginx + uvicorn), GitHub Actions
- **Tower data:** FCC TV/FM Query for `us`, [Maprad.io](https://maprad.io) GraphQL
  for `au` (ACMA RRL) and `ca` (ISED SMS) — Maprad also supplements US results
  when `MAPRAD_API_KEY` is set. Elevation from Open-Meteo.

## Where to go next

- [`ONBOARDING.md`](ONBOARDING.md) — full local setup, tests, how code ships.
- [`docs/architecture.md`](docs/architecture.md) — system overview, data flow, storage.
- [`docs/pipeline.md`](docs/pipeline.md) — detection → tracker → geolocator → aircraft JSON.
- [`docs/runbook.md`](docs/runbook.md) — production operations and incident response.
