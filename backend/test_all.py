"""
Full integration tests for Tower Finder API.
Run with: python test_all.py
"""
import json
import sys
import httpx

BASE = "http://localhost:8000"
PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
errors = []


def check(name, condition, detail=""):
    if condition:
        print(f"  {PASS} {name}")
    else:
        print(f"  {FAIL} {name}" + (f" — {detail}" if detail else ""))
        errors.append(name)


def section(title):
    print(f"\n{'='*50}\n  {title}\n{'='*50}")


# ─── 1. Health ────────────────────────────────────────────────────────────────
section("1. Health")
r = httpx.get(f"{BASE}/api/health")
check("Status 200", r.status_code == 200)
check("Body ok", r.json() == {"status": "ok"})


# ─── 2. Config GET ────────────────────────────────────────────────────────────
section("2. Config GET")
r = httpx.get(f"{BASE}/api/config")
check("Status 200", r.status_code == 200)
cfg = r.json()
check("Has ranking key", "ranking" in cfg)
check("Has band_priority", "band_priority" in cfg["ranking"])
check("Has distance_classes", "distance_classes" in cfg["ranking"])
check("Has sort_order", "sort_order" in cfg["ranking"])
check("Has receiver settings", "receiver" in cfg)
check("Has broadcast_bands", "broadcast_bands" in cfg)
check("VHF default priority 0", cfg["ranking"]["band_priority"].get("VHF") == 0)
check("FM default priority 2", cfg["ranking"]["band_priority"].get("FM") == 2)


# ─── 3. Config PUT + live reload ──────────────────────────────────────────────
section("3. Config PUT (live reload)")
new_cfg = json.loads(json.dumps(cfg))
new_cfg["ranking"]["band_priority"]["FM"] = 0
new_cfg["ranking"]["band_priority"]["VHF"] = 2

r = httpx.put(f"{BASE}/api/config", json=new_cfg)
check("PUT status 200", r.status_code == 200)
check("PUT returns updated", r.json().get("status") == "updated")

# Verify the reload
r2 = httpx.get(f"{BASE}/api/config")
reloaded = r2.json()
check("FM now priority 0 after reload", reloaded["ranking"]["band_priority"].get("FM") == 0)
check("VHF now priority 2 after reload", reloaded["ranking"]["band_priority"].get("VHF") == 2)

# Restore original
r3 = httpx.put(f"{BASE}/api/config", json=cfg)
check("Config restored", r3.status_code == 200)
r4 = httpx.get(f"{BASE}/api/config")
check("VHF back to 0", r4.json()["ranking"]["band_priority"].get("VHF") == 0)


# ─── 4. Elevation API ─────────────────────────────────────────────────────────
section("4. Elevation lookup (/api/elevation)")

# Sydney harbour
r = httpx.get(f"{BASE}/api/elevation", params={"lat": -33.8688, "lon": 151.2093}, timeout=15)
check("Status 200", r.status_code == 200)
body = r.json()
check("Has elevation_m key", "elevation_m" in body)
check("Elevation is a number", isinstance(body.get("elevation_m"), (int, float)))
check("Sydney elevation plausible (0–200m)", 0 <= body.get("elevation_m", -1) <= 200,
      f"got {body.get('elevation_m')}")
print(f"     Sydney elevation: {body.get('elevation_m')} m")

# Denver (high altitude)
r = httpx.get(f"{BASE}/api/elevation", params={"lat": 39.7392, "lon": -104.9903}, timeout=15)
body = r.json()
check("Denver elevation > 1500m", body.get("elevation_m", 0) > 1500,
      f"got {body.get('elevation_m')}")
print(f"     Denver elevation: {body.get('elevation_m')} m")

# Validation errors
r = httpx.get(f"{BASE}/api/elevation", params={"lat": 999, "lon": 0})
check("lat=999 → 422", r.status_code == 422)


# ─── 5. Auto source detection ─────────────────────────────────────────────────
section("5. Auto database source detection (/api/towers?source=auto)")

# We can't make real tower searches without spending API quota,
# so we test the _detect_source logic directly in Python
import importlib, sys as _sys
_sys.path.insert(0, "/Users/admin/Tower-Finder/backend")
from main import _detect_source  # noqa

check("Sydney → au",  _detect_source(-33.8688, 151.2093) == "au")
check("Washington DC → us", _detect_source(38.8977, -77.0365) == "us")
check("Toronto → ca",  _detect_source(43.6532, -79.3832) == "ca")
check("Anchorage → us", _detect_source(61.2181, -149.9003) == "us")
check("Honolulu → us", _detect_source(21.3069, -157.8583) == "us")
check("Unknown (0, 0) → us fallback", _detect_source(0, 0) == "us")


# ─── 6. Towers endpoint — validation ─────────────────────────────────────────
section("6. /api/towers — parameter validation")

r = httpx.get(f"{BASE}/api/towers")  # missing lat/lon
check("Missing lat/lon → 422", r.status_code == 422)

r = httpx.get(f"{BASE}/api/towers", params={"lat": 999, "lon": 0})
check("lat out of range → 422", r.status_code == 422)

r = httpx.get(f"{BASE}/api/towers", params={"lat": 0, "lon": 0, "source": "xx"})
check("Invalid source → 400", r.status_code == 400)


# ─── 7. ResultsTable columns ──────────────────────────────────────────────────
section("7. Frontend ResultsTable columns")
with open("/Users/admin/Tower-Finder/frontend/src/components/ResultsTable.jsx") as f:
    jsx = f.read()
check("Lat column header", "<th>Lat</th>" in jsx)
check("Long column header", "<th>Long</th>" in jsx)
check("Altitude column header", "<th>Altitude (m)</th>" in jsx)
check("Ant. Height column header", "<th>Ant. Height (m)</th>" in jsx)
check("latitude field rendered", "t.latitude" in jsx)
check("longitude field rendered", "t.longitude" in jsx)
check("altitude_m rendered", "t.altitude_m" in jsx)
check("antenna_height_m rendered", "t.antenna_height_m" in jsx)


# ─── 8. SearchForm auto-detection ────────────────────────────────────────────
section("8. Frontend SearchForm auto-detection & elevation")
with open("/Users/admin/Tower-Finder/frontend/src/components/SearchForm.jsx") as f:
    jsx = f.read()
check("detectSource function exists", "function detectSource" in jsx)
check("Australia bounding box", "lon >= 112 && lon <= 155" in jsx)
check("Canada bounding box", "lon >= -141" in jsx)
check("useEffect for source detection", "useEffect" in jsx)
check("Elevation auto-fetch effect", "fetchElevation" in jsx)
check("altitudeManual ref used", "altitudeManual" in jsx)
check("Placeholder updated", "Auto-detected" in jsx)


# ─── 9. calculations.py config ───────────────────────────────────────────────
section("9. calculations.py — config-driven ranking")
with open("/Users/admin/Tower-Finder/backend/calculations.py") as f:
    py = f.read()
check("tower_config.json loaded", "tower_config.json" in py)
check("reload_config exists", "def reload_config" in py)
check("SORT_ORDER used in sort", "SORT_ORDER" in py)
check("No hard-coded BAND_PRIORITY dict literal", "BAND_PRIORITY = {" not in py)
check("DEFAULT_RADIUS_KM exported", "DEFAULT_RADIUS_KM" in py)
check("DEFAULT_LIMIT exported", "DEFAULT_LIMIT" in py)

# Functional test — change config and verify sort changes
import json as _json
cfg_path = "/Users/admin/Tower-Finder/backend/tower_config.json"
with open(cfg_path) as f:
    orig = _json.load(f)

from calculations import process_and_rank, reload_config
from calculations import BAND_PRIORITY as BP_before
from calculations import DEFAULT_RADIUS_KM as RADIUS_before
from calculations import DEFAULT_LIMIT as LIMIT_before
check("VHF priority 0 before change", BP_before.get("VHF") == 0)
check("Default radius is 80", RADIUS_before == 80)
check("Default limit is 20", LIMIT_before == 20)

# Swap priorities + change radius
modified = _json.loads(_json.dumps(orig))
modified["ranking"]["band_priority"]["VHF"] = 1
modified["ranking"]["band_priority"]["UHF"] = 0
modified["search"]["default_radius_km"] = 100
modified["search"]["default_limit"] = 15
with open(cfg_path, "w") as f:
    _json.dump(modified, f, indent=2)
reload_config()
from calculations import BAND_PRIORITY as BP_after
from calculations import DEFAULT_RADIUS_KM as RADIUS_after
from calculations import DEFAULT_LIMIT as LIMIT_after
check("UHF priority 0 after reload", BP_after.get("UHF") == 0)
check("VHF priority 1 after reload", BP_after.get("VHF") == 1)
check("Radius changed to 100", RADIUS_after == 100)
check("Limit changed to 15", LIMIT_after == 15)

# Restore
with open(cfg_path, "w") as f:
    _json.dump(orig, f, indent=2)
reload_config()
from calculations import BAND_PRIORITY as BP_restored
check("VHF priority 0 after restore", BP_restored.get("VHF") == 0)


# ─── 10. Deployment files ─────────────────────────────────────────────────────
section("10. Deployment files exist")
import os
check("Dockerfile exists", os.path.exists("/Users/admin/Tower-Finder/Dockerfile"))
check("docker-compose.yml exists", os.path.exists("/Users/admin/Tower-Finder/docker-compose.yml"))
check(".dockerignore exists", os.path.exists("/Users/admin/Tower-Finder/.dockerignore"))
check("deploy/nginx.conf exists", os.path.exists("/Users/admin/Tower-Finder/deploy/nginx.conf"))
check("deploy/start.sh exists", os.path.exists("/Users/admin/Tower-Finder/deploy/start.sh"))
check("deploy/DEPLOY.md exists", os.path.exists("/Users/admin/Tower-Finder/deploy/DEPLOY.md"))

with open("/Users/admin/Tower-Finder/Dockerfile") as f:
    df = f.read()
check("Dockerfile has multi-stage build", "frontend-build" in df)
check("Dockerfile uses nginx", "nginx" in df)
check("Dockerfile exposes 80", "EXPOSE 80" in df)

with open("/Users/admin/Tower-Finder/docker-compose.yml") as f:
    dc = f.read()
check("docker-compose has healthcheck", "healthcheck" in dc)
check("docker-compose uses .env file", ".env" in dc)


# ─── 11. Configurable radius & limit in API ──────────────────────────────────
section("11. Configurable search radius & limit")
with open("/Users/admin/Tower-Finder/backend/main.py") as f:
    main_py = f.read()
check("radius_km query param in towers endpoint", "radius_km" in main_py)
check("effective_radius from config", "effective_radius" in main_py)
check("effective_limit from config", "effective_limit" in main_py)
check("DEFAULT_RADIUS_KM imported", "DEFAULT_RADIUS_KM" in main_py)
check("DEFAULT_LIMIT imported", "DEFAULT_LIMIT" in main_py)
check("radius_km passed to fetch_broadcast_systems", "radius_km=effective_radius" in main_py)


# ─── 12. CORS from environment ───────────────────────────────────────────────
section("12. CORS origins configurable via env")
check("CORS_ORIGINS env var read", "CORS_ORIGINS" in main_py)
check("_CORS_ORIGINS variable", "_CORS_ORIGINS" in main_py)
check("allow_origins uses variable", "allow_origins=_CORS_ORIGINS" in main_py)


# ─── 13. Tower usage statistics ──────────────────────────────────────────────
section("13. Tower usage statistics")
check("POST stats endpoint exists", "/api/stats/tower-selection" in main_py)
check("GET stats summary exists", "/api/stats/summary" in main_py)
check("tower_stats.json path defined", "tower_stats.json" in main_py)

# Test POST — record a selection
r = httpx.post(f"{BASE}/api/stats/tower-selection", json={
    "node_id": "test-node-1",
    "tower_callsign": "ABC7",
    "tower_frequency_mhz": 177.5,
    "tower_lat": -33.8,
    "tower_lon": 151.2,
    "node_lat": -33.9,
    "node_lon": 151.1,
    "source": "au",
})
check("POST selection → 200", r.status_code == 200)
check("POST returns recorded", r.json().get("status") == "recorded")

# Test a second selection
r2 = httpx.post(f"{BASE}/api/stats/tower-selection", json={
    "node_id": "test-node-2",
    "tower_callsign": "ABC7",
    "tower_frequency_mhz": 177.5,
    "tower_lat": -33.8,
    "tower_lon": 151.2,
    "node_lat": -34.0,
    "node_lon": 151.0,
    "source": "au",
})
check("Second POST → 200", r2.status_code == 200)

# Test validation — missing required fields
r3 = httpx.post(f"{BASE}/api/stats/tower-selection", json={"node_id": "x"})
check("Missing fields → 400", r3.status_code == 400)

# Test GET summary
r4 = httpx.get(f"{BASE}/api/stats/summary")
check("GET summary → 200", r4.status_code == 200)
summary = r4.json()
check("Summary has total_selections", "total_selections" in summary)
check("Summary has unique_towers", "unique_towers" in summary)
check("Summary has tower_usage list", isinstance(summary.get("tower_usage"), list))
check("Total selections >= 2", summary.get("total_selections", 0) >= 2)
print(f"     Total selections: {summary.get('total_selections')}, unique towers: {summary.get('unique_towers')}")

# Cleanup test stats file
stats_path = "/Users/admin/Tower-Finder/backend/tower_stats.json"
if os.path.exists(stats_path):
    os.remove(stats_path)
    check("Test stats file cleaned up", not os.path.exists(stats_path))


# ─── 14. Tower elevation enrichment ──────────────────────────────────────────
section("14. Tower elevation enrichment")
with open("/Users/admin/Tower-Finder/backend/main.py") as f:
    main_py2 = f.read()
check("batch_lookup_elevations exists", "_batch_lookup_elevations" in main_py2)
check("elevation_m added to towers", 'elevation_m' in main_py2)
check("altitude_m added to towers", 'altitude_m' in main_py2)

# Test batch elevation lookup directly
import asyncio
from main import _batch_lookup_elevations

coords = [(-33.8688, 151.2093), (39.7392, -104.9903)]
result = asyncio.run(_batch_lookup_elevations(coords))
check("Batch returns dict", isinstance(result, dict))
check("Batch returned 2 results", len(result) >= 2, f"got {len(result)}")
sydney_key = (-33.8688, 151.2093)
check("Sydney elevation plausible", 0 <= result.get(sydney_key, -1) <= 200, f"got {result.get(sydney_key)}")
denver_key = (39.7392, -104.9903)
check("Denver elevation > 1500m", result.get(denver_key, 0) > 1500, f"got {result.get(denver_key)}")

# Test with empty list
empty_result = asyncio.run(_batch_lookup_elevations([]))
check("Empty coords returns empty dict", empty_result == {})


# ─── Summary ──────────────────────────────────────────────────────────────────
section("SUMMARY")
total = 0
with open(__file__) as f:
    for line in f:
        if line.strip().startswith("check("):
            total += 1

passed = total - len(errors)
print(f"\n  Passed: {passed}/{total}")
if errors:
    print(f"\n  Failed:")
    for e in errors:
        print(f"    {FAIL} {e}")
    sys.exit(1)
else:
    print(f"\n  {PASS} All tests passed!")
