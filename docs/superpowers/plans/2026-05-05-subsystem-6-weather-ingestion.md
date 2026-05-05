# Subsystem 6 — Open-Meteo weather ingestion

**Goal:** Sample current-conditions weather hourly at each connected node's receiver location and persist as a parallel Parquet stream so analysts can join it against detections by `node_id` + hour.

**Architecture:**
- `services/weather_client.py` — Open-Meteo HTTP client (free, no API key).
- `services/weather_writer.py` — Parquet writer for weather samples.
- `services/tasks/weather_archive.py` — hourly background task: snapshots `state.connected_nodes`, fetches Open-Meteo for each `rx_lat/rx_lon`, accumulates samples, flushes once per hour to Parquet.
- Path: `weather/year=YYYY/month=MM/day=DD/node_id=XXX/hourly.parquet`.

**Tech Stack:** `httpx` (already a dep).

**Schema:**
```
sample_ts_ms   int64       open-meteo's "time" for the current observation
fetch_ts_ms    int64       wallclock when we fetched
node_id        string
lat            float64
lon            float64
temperature_c  float64 nullable
humidity_pct   float64 nullable
pressure_hpa   float64 nullable
precipitation_mm float64 nullable
wind_speed_ms  float64 nullable
wind_dir_deg   float64 nullable
cloud_cover_pct float64 nullable
visibility_m   float64 nullable
weather_code   int32 nullable
```
