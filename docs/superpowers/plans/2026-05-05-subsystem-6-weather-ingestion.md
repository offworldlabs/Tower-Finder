# Subsystem 6 — Weather: on-demand helper (originally: ingestion task)

**Status:** Original ingestion-task design was reverted in response to PR feedback. Open-Meteo offers a free historical archive API (`https://archive-api.open-meteo.com/v1/archive`) that returns the same hourly observations on demand given a (lat, lon, date range), so writing our own background task and storing a parallel Parquet stream would just be duplicating data already on tap.

**Final design:**
- `backend/analysis/weather.py` exposes `fetch_historical(lat, lon, start_dt, end_dt)` that hits the archive API and returns a list of per-hour dicts in the same flat shape used elsewhere in the project (`temperature_c`, `humidity_pct`, `pressure_hpa`, `precipitation_mm`, `wind_speed_ms`, `wind_dir_deg`, `cloud_cover_pct`, `visibility_m`, `weather_code`).
- No background task, no extra Parquet stream, no scheduled fetches.
- Analysts join weather at query time from notebooks / pipelines.

**What was deleted:**
- `services/weather_client.py` (was: forecast/current endpoint)
- `services/weather_writer.py` (was: per-node hourly Parquet writer)
- `services/tasks/weather_archive.py` (was: hourly background task)
- `tests/test_weather_archive.py`
- `weather_archive_task` wiring from `main.py`, `services/tasks/__init__.py`, `services/background.py`, `core/task_registry.py`.

**What stays:** `analysis/weather.py` + `tests/test_analysis_weather.py`.
