# Tower Finder / RETINA

This repo powers RETINA, a passive-radar system: receiver nodes detect aircraft
from reflections of broadcast transmitters, and the backend turns those
detections into live tracks shown on a web map. "Tower Finder" — the original
illuminator-search feature documented below — is one of several surfaces (along
with the live map and the admin dashboard).

> **New here?** Start with [`ONBOARDING.md`](ONBOARDING.md) for the full picture
> and local setup, and [`docs/architecture.md`](docs/architecture.md) for how the
> pieces fit together.

## Tower Finder feature

Web application and API that helps passive radar operators find suitable broadcast tower illuminators near their location.

Given geographic coordinates, the system queries the [Maprad.io](https://maprad.io) transmitter database for nearby FM/VHF/UHF broadcast towers, then filters and ranks them by suitability for passive radar use.

## Project Structure

```
backend/          Python API (FastAPI)
frontend/         React SPA (Vite)
dashboard/        Admin dashboard (React/Vite)
libs/             Git submodules
  retina-geolocator/   Bistatic passive radar geolocation solver
  retina-tracker/      Multi-target Kalman tracker with anomaly detection
  retina-custody/      Chain-of-custody signing for node detections
  retina-analytics/    Coverage and detection analytics
  retina-simulation/   Synthetic node fleet and world model
```

## Quick Start

### Clone (with submodules)

```bash
git clone --recursive https://github.com/offworldlabs/Tower-Finder.git
cd Tower-Finder

# If already cloned without --recursive:
git submodule update --init --recursive
```

### Backend

```bash
just setup
```

That is the supported path: it initialises the submodules, builds the backend venv
with `uv`, installs all five `libs/` packages editable, seeds `backend/.env` from
the example, and installs the frontend dependencies. Install all five even if you
only care about tower search: `retina-simulation` imports the other four, so a
partial install fails at import time rather than at use.

Then either run the whole local stack:

```bash
just up      # uvicorn + synthetic fleet + Vite, with hot reload
just status  # what is alive
just down    # stop it
```

or just the API on its own:

```bash
cd backend && .venv/bin/uvicorn main:app --reload
```

The API runs at `http://localhost:8000`. Interactive docs at `/docs`. Add your
Maprad.io API key to `backend/.env` for tower search; the live map does not need
it.

#### Database migrations

The schema is owned by Alembic (`backend/migrations/`). `create_all` runs only
in the test suite, which sets `RETINA_SCHEMA_SOURCE=create_all`; everywhere else
`deploy/start.sh` applies migrations on boot.

To change the schema, edit the models, then from `backend/`:

```bash
uv run alembic revision --autogenerate -m "what changed"
uv run alembic upgrade head
```

Review the generated file before committing. Anything other than `create_table`
must go through `op.batch_alter_table`, because SQLite cannot `ALTER`.
`RETINA_DB_PATH` points Alembic at a scratch file if you want to try a
migration without touching `backend/data/users.db`.

### Frontend

`just setup` already installed the dependencies, and `just up` runs this alongside
the backend. To run it on its own:

```bash
cd frontend && npm run dev
```

Opens at `http://localhost:5173`. API calls are proxied to the backend during
development.

## API

### `GET /api/towers`

| Parameter  | Type   | Required | Default | Description                             |
|------------|--------|----------|---------|-----------------------------------------|
| `lat`      | float  | yes      |         | Latitude (-90 to 90)                    |
| `lon`      | float  | yes      |         | Longitude (-180 to 180)                 |
| `altitude` | float  | no       | 0       | Receiver altitude in metres             |
| `limit`    | int    | no       | 20      | Max towers to return (1–100)            |
| `source`   | string | no       | au      | Data source: `au`, `us`, `ca`           |

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
      "distance_km": 5.9,
      "bearing_deg": 337.5,
      "bearing_cardinal": "NNW",
      "received_power_dbm": -7.7,
      "distance_class": "Too Close",
      "eirp_dbm": 79.1,
      "licence_type": "Broadcasting",
      "licence_subtype": "Commercial Television"
    }
  ],
  "query": { "latitude": -33.8688, "longitude": 151.2093, "altitude_m": 0, "radius_km": 80, "source": "au" },
  "count": 20
}
```

## How Ranking Works

1. Fetch all FM, VHF and UHF transmitters within 80 km from Maprad.io
2. Discard towers whose estimated received power is below −95 dBm
3. Classify each tower by band (VHF / UHF / FM) and distance suitability:
   - **Too Close** (< 8 km) — direct signal may overwhelm the receiver
   - **Ideal** (8–30 km) — best bistatic geometry
   - **Good** (30–60 km) — workable
   - **Far** (> 60 km) — fallback only
4. Rank by: band preference (VHF → UHF → FM) → distance class → signal strength
5. Return top N results

## Tech Stack

- **Backend:** Python 3.11+, FastAPI, httpx
- **Frontend:** React 18, Vite, Leaflet
- **Data source:** Maprad.io GraphQL API (ACMA RRL, FCC ULS, ISED SMS)
