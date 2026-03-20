# Tower Finder

Passive radar network platform — find broadcast tower illuminators, ingest IQ data from distributed nodes, process detections, and visualize aircraft tracks in real time.

Given geographic coordinates, the system queries the [Maprad.io](https://maprad.io) transmitter database for nearby FM/VHF/UHF broadcast towers, then filters and ranks them by suitability for passive radar use. A distributed node network streams IQ frames over TCP for server-side passive radar processing, with results displayed on a live aircraft map and managed through admin/user dashboards.

## Project Structure

```
backend/          Python API (FastAPI) — tower search, radar pipeline, analytics, auth
frontend/         React SPA (Vite) — live aircraft map + tower finder UI
dashboard/        React SPA (Vite) — operator & admin dashboards
deploy/           Nginx config, startup scripts
Dockerfile        Multi-stage build (frontend + dashboard + backend)
```

## Quick Start

### Docker (recommended)

```bash
cp backend/.env.example backend/.env   # configure API keys and secrets
docker compose up --build
```

This builds and starts the full stack (backend, frontend, dashboard, nginx) on port 80. The radar TCP server listens on port 3012.

### Local Development

**Backend:**

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # add your Maprad.io API key + auth secrets
uvicorn main:app --reload
```

The API runs at `http://localhost:8000`. Interactive docs at `/docs`.

**Frontend (aircraft map):**

```bash
cd frontend
npm install
npm run dev
```

Opens at `http://localhost:5173`.

**Dashboard:**

```bash
cd dashboard
npm install
npm run dev
```

Opens at `http://localhost:5174`. The dashboard serves two modes based on hostname: `dash.*` for operators and `admin.*` for administrators (or use `?mode=admin`).

## Authentication

OAuth login via Google or GitHub. Configure in `backend/.env`:

```
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GITHUB_CLIENT_ID=...
GITHUB_CLIENT_SECRET=...
JWT_SECRET=change-me-in-production
AUTH_ADMIN_EMAILS=admin@retina.fm
```

When no OAuth client IDs are set, authentication is bypassed for local development.

## Dashboard

The dashboard (`dashboard/`) is a separate React app with role-based views:

**User pages:** Network overview, node detail, detections, contribution stats, data explorer (paginated archive browser), settings.

**Admin pages:** Network health, node management, analytics, events, storage, chain-of-custody, user management, configuration.

## Passive Radar Pipeline

The backend runs a full passive radar signal processing pipeline:

1. Distributed nodes stream IQ frames to the TCP server (port 3012)
2. Parallel frame processor workers (configurable via `FRAME_WORKERS`, default 4) run matched filtering and detection
3. Detections are correlated with ADS-B truth data from OpenSky
4. Node reputation and trust scores are computed continuously
5. Results are written to tar1090-compatible JSON for the live map and archived to B2 storage

## API

### `GET /api/towers`

| Parameter  | Type   | Required | Default | Description                             |
|------------|--------|----------|---------|-----------------------------------------|
| `lat`      | float  | yes      |         | Latitude (-90 to 90)                    |
| `lon`      | float  | yes      |         | Longitude (-180 to 180)                 |
| `altitude` | float  | no       | 0       | Receiver altitude in metres             |
| `limit`    | int    | no       | 20      | Max towers to return (1–100)            |
| `source`   | string | no       | au      | Data source: `au`, `us`, `ca`           |

Additional API routes: `/api/stats`, `/api/radar`, `/api/analytics`, `/api/streaming`, `/api/archive`, `/api/custody`, `/api/auth`, `/api/admin`. See `/docs` for full OpenAPI reference.

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

- **Backend:** Python 3.12, FastAPI, NumPy, SciPy, httpx, PyJWT
- **Frontend:** React 18, Vite, Leaflet
- **Dashboard:** React 18, Vite, Recharts, React Router, Leaflet
- **Infrastructure:** Docker, Nginx, Backblaze B2
- **Data sources:** Maprad.io GraphQL API (ACMA RRL, FCC ULS, ISED SMS), OpenSky ADS-B
