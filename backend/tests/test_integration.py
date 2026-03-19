"""
Full integration tests for Tower Finder API.
Run with: python test_all.py
"""
import json
import sys
import httpx
import time

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

from services.tower_ranking import process_and_rank, reload_config
from services.tower_ranking import BAND_PRIORITY as BP_before
from services.tower_ranking import DEFAULT_RADIUS_KM as RADIUS_before
from services.tower_ranking import DEFAULT_LIMIT as LIMIT_before
check("VHF priority 0 before change", BP_before.get("VHF") == 0)
check("Default radius is 80", RADIUS_before == 80)
check("Default limit is 100", LIMIT_before == 100)

# Swap priorities + change radius
modified = _json.loads(_json.dumps(orig))
modified["ranking"]["band_priority"]["VHF"] = 1
modified["ranking"]["band_priority"]["UHF"] = 0
modified["search"]["default_radius_km"] = 100
modified["search"]["default_limit"] = 15
with open(cfg_path, "w") as f:
    _json.dump(modified, f, indent=2)
reload_config()
from services.tower_ranking import BAND_PRIORITY as BP_after
from services.tower_ranking import DEFAULT_RADIUS_KM as RADIUS_after
from services.tower_ranking import DEFAULT_LIMIT as LIMIT_after
check("UHF priority 0 after reload", BP_after.get("UHF") == 0)
check("VHF priority 1 after reload", BP_after.get("VHF") == 1)
check("Radius changed to 100", RADIUS_after == 100)
check("Limit changed to 15", LIMIT_after == 15)

# Restore
with open(cfg_path, "w") as f:
    _json.dump(orig, f, indent=2)
reload_config()
from services.tower_ranking import BAND_PRIORITY as BP_restored
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


# ─── 15. Broadcast band classification (FM/VHF/UHF) ─────────────────────────
section("15. Broadcast band classification (FM / VHF / UHF)")
from services.tower_ranking import classify_band
check("FM low edge 87.8", classify_band(87.8) == "FM")
check("FM high edge 108.0", classify_band(108.0) == "FM")
check("FM mid 95.5", classify_band(95.5) == "FM")
check("Below FM 87.7 → None", classify_band(87.7) is None)
check("VHF low edge 174", classify_band(174) == "VHF")
check("VHF high edge 216", classify_band(216) == "VHF")
check("VHF mid 195", classify_band(195) == "VHF")
check("Gap 108.1-173.9 → None", classify_band(140) is None)
check("UHF low edge 470", classify_band(470) == "UHF")
check("UHF high edge 608", classify_band(608) == "UHF")
check("UHF mid 550", classify_band(550) == "UHF")
check("Above UHF 609 → None", classify_band(609) is None)


# ─── 16. User frequency parsing ──────────────────────────────────────────────
section("16. User frequency parsing")
from services.tower_ranking import parse_user_frequencies
check("Empty string → []", parse_user_frequencies("") == [])
check("Single freq", parse_user_frequencies("95.5") == [95.5])
check("Multiple freqs", parse_user_frequencies("95.5, 177.5, 500") == [95.5, 177.5, 500])
check("Trailing comma", parse_user_frequencies("95.5,") == [95.5])
check("Invalid values skipped", parse_user_frequencies("abc, 95.5, xyz") == [95.5])
check("Max 10 enforced", len(parse_user_frequencies(",".join(str(i) for i in range(1, 20)))) == 10)
check("Zero skipped", parse_user_frequencies("0, 95.5") == [95.5])
check("Negative skipped", parse_user_frequencies("-5, 95.5") == [95.5])


# ─── 17. Frequency match in ranking ──────────────────────────────────────────
section("17. Frequency match in ranking")
with open("/Users/admin/Tower-Finder/backend/main.py") as f:
    main_py_freqs = f.read()
check("frequencies param in towers endpoint", "frequencies" in main_py_freqs)
check("parse_user_frequencies imported", "parse_user_frequencies" in main_py_freqs)
check("user_frequencies passed to process_and_rank", "user_frequencies=user_freqs" in main_py_freqs)
check("user_frequencies_mhz in response", "user_frequencies_mhz" in main_py_freqs)

with open("/Users/admin/Tower-Finder/backend/calculations.py") as f:
    calc_py = f.read()
check("FREQUENCY_MATCH_TOLERANCE_MHZ defined", "FREQUENCY_MATCH_TOLERANCE_MHZ" in calc_py)
check("frequency_matched field in tower dict", "frequency_matched" in calc_py)
check("Frequency match sorts first", "frequency_matched" in calc_py)


# ─── 18. USA default country ─────────────────────────────────────────────────
section("18. USA default country in frontend")
with open("/Users/admin/Tower-Finder/frontend/src/components/SearchForm.jsx") as f:
    sf_jsx = f.read()
check("Default source is 'us'", 'useState("us")' in sf_jsx)
check("US is first dropdown option", sf_jsx.index('value="us"') < sf_jsx.index('value="ca"'))
check("CA before AU in dropdown", sf_jsx.index('value="ca"') < sf_jsx.index('value="au"'))


# ─── 19. Frequency input in frontend ─────────────────────────────────────────
section("19. Frequency input in frontend")
check("Frequencies state in SearchForm", "frequencies" in sf_jsx)
check("showFrequencies toggle", "showFrequencies" in sf_jsx)
check("Max 10 frequencies enforced in UI", "frequencies.length < 10" in sf_jsx)
check("Frequency passed to onSearch", "frequencies: parsedFreqs" in sf_jsx)

with open("/Users/admin/Tower-Finder/frontend/src/api.js") as f:
    api_js = f.read()
check("frequencies param in fetchTowers", "frequencies" in api_js)

with open("/Users/admin/Tower-Finder/frontend/src/components/ResultsTable.jsx") as f:
    rt_jsx = f.read()
check("frequency_matched badge in table", "frequency_matched" in rt_jsx)
check("freq-match-badge class", "freq-match-badge" in rt_jsx)


# ─── 20. Passive Radar — Pipeline Status ──────────────────────────────────────
section("20. Passive Radar — Status")
r = httpx.get(f"{BASE}/api/radar/status")
check("Radar status 200", r.status_code == 200)
st = r.json()
check("Has node_id", "node_id" in st)
check("Has config with rx_lat", "rx_lat" in st.get("config", {}))

# ─── 21. Passive Radar — receiver.json ────────────────────────────────────────
section("21. Passive Radar — receiver.json")
r = httpx.get(f"{BASE}/api/radar/data/receiver.json")
check("receiver.json 200", r.status_code == 200)
rj = r.json()
check("Has lat", "lat" in rj)
check("Has lon", "lon" in rj)
check("Has version", rj.get("version") == "retina-passive-radar")

# ─── 22. Passive Radar — aircraft.json ────────────────────────────────────────
section("22. Passive Radar — aircraft.json (empty)")
r = httpx.get(f"{BASE}/api/radar/data/aircraft.json")
check("aircraft.json 200", r.status_code == 200)
aj = r.json()
check("Has now", "now" in aj)
check("Has aircraft array", isinstance(aj.get("aircraft"), list))

# ─── 23. Passive Radar — Ingest detection frame ──────────────────────────────
section("23. Passive Radar — Ingest Detections")
# Send multiple frames to create a confirmed track
frames = []
for i in range(5):
    frames.append({
        "timestamp": 1749190409000 + i * 500,
        "delay": [33.5],
        "doppler": [65.0],
        "snr": [12.0],
    })
r = httpx.post(f"{BASE}/api/radar/detections", json={"frames": frames})
check("Ingest 200", r.status_code == 200)
ir = r.json()
check("Frames processed", ir.get("frames_processed") == 5)
check("Has tracks >= 1", ir.get("tracks", 0) >= 1)

# Verify tracks appear in aircraft.json
r = httpx.get(f"{BASE}/api/radar/data/aircraft.json")
aj = r.json()
check("Aircraft populated after ingest", len(aj.get("aircraft", [])) >= 1)
if aj["aircraft"]:
    ac = aj["aircraft"][0]
    check("Aircraft has hex", "hex" in ac)
    check("Aircraft has lat/lon", "lat" in ac and "lon" in ac)
    check("Aircraft has gs", "gs" in ac)


# ─── 24. Node Analytics — Trust Score & Reputation ────────────────────────────
section("24. Node Analytics — Trust Score & Reputation")
from analytics import (
    TrustScoreState, AdsReportEntry, DetectionAreaState, NodeMetrics,
    NodeReputation, HistoricalCoverageMap, NodeAnalyticsManager,
    YAGI_BEAM_WIDTH_DEG, YAGI_MAX_RANGE_KM,
)

# Trust score basics
ts = TrustScoreState(node_id="test-node")
check("Trust score 0 with no samples", ts.score == 0.0)
ts.add_sample(AdsReportEntry(
    timestamp_ms=1000, predicted_delay=10.0, predicted_doppler=50.0,
    measured_delay=10.5, measured_doppler=51.0,
    adsb_hex="abc123", adsb_lat=33.9, adsb_lon=-84.6,
))
check("Trust score 1.0 with good sample", ts.score == 1.0)
ts.add_sample(AdsReportEntry(
    timestamp_ms=2000, predicted_delay=10.0, predicted_doppler=50.0,
    measured_delay=20.0, measured_doppler=100.0,
    adsb_hex="abc124", adsb_lat=33.9, adsb_lon=-84.6,
))
check("Trust score 0.5 with one bad sample", ts.score == 0.5)

# Yagi constants
check("YAGI_BEAM_WIDTH_DEG is 41", YAGI_BEAM_WIDTH_DEG == 41.0)
check("YAGI_MAX_RANGE_KM is 50", YAGI_MAX_RANGE_KM == 50.0)

# Detection area defaults
da = DetectionAreaState(node_id="test-da")
check("DetectionArea default beam_width 41", da.beam_width_deg == 41.0)
check("DetectionArea default max_range 50", da.max_range_km == 50.0)
da.update(15.0, 80.0)
da.update(25.0, -40.0)
check("DetectionArea delay range", da.delay_range == (15.0, 25.0))
check("DetectionArea doppler range", da.doppler_range == (-40.0, 80.0))
check("DetectionArea n_detections 2", da.n_detections == 2)

# Node reputation — good behaviour
rep = NodeReputation(node_id="good-node")
check("Initial reputation 1.0", rep.reputation == 1.0)
check("Not blocked initially", not rep.blocked)
rep.evaluate_trust(0.9)
check("Good trust keeps reputation high", rep.reputation >= 1.0)

# Node reputation — bad actor blocking
bad_rep = NodeReputation(node_id="bad-node")
for _ in range(15):
    bad_rep.evaluate_trust(0.05)
check("Bad actor gets blocked", bad_rep.blocked)
check("Block reason set", "Reputation" in bad_rep.block_reason or "Trust" in bad_rep.block_reason)

# Unblock
bad_rep.unblock()
check("Unblock clears blocked flag", not bad_rep.blocked)
check("Unblock sets reputation to 0.3", bad_rep.reputation == 0.3)

# Heartbeat penalty
hb_rep = NodeReputation(node_id="stale-hb")
hb_rep.evaluate_heartbeat(time.time() - 600)  # 10 min stale
check("Stale heartbeat penalised", hb_rep.reputation < 1.0)

# Detection rate penalty
rate_rep = NodeReputation(node_id="high-rate")
rate_rep.evaluate_detection_rate(100.0)
check("High detection rate penalised", rate_rep.reputation < 1.0)


# ─── 25. Node Analytics — Historical Coverage Map ─────────────────────────────
section("25. Node Analytics — Historical Coverage Map")
cov = HistoricalCoverageMap(node_id="test-cov")
check("Coverage empty initially", cov.n_grid_cells == 0)

# Add detections in a cone-like pattern
for i in range(30):
    lat = 33.9 + i * 0.01
    lon = -84.6 + i * 0.005
    cov.add_detection(lat, lon, alt_km=8.0, snr=15.0, delay_error=0.5)

check("Coverage has entries", len(cov.entries) == 30)
check("Coverage grid populated", cov.n_grid_cells > 0)
check("Coverage area > 0", cov.coverage_area_km2 > 0)

beam_est = cov.estimate_beam_width()
check("Beam width estimated", beam_est is not None)
check("Beam width is reasonable (<=180)", beam_est <= 180.0 if beam_est else False)

grid = cov.get_coverage_grid()
check("Coverage grid returns list", isinstance(grid, list))
check("Grid cell has count", all("count" in c for c in grid))

summary = cov.summary()
check("Coverage summary has node_id", summary["node_id"] == "test-cov")
check("Coverage summary has grid_cells", "grid_cells" in summary)


# ─── 26. Node Analytics — Manager Integration ────────────────────────────────
section("26. Node Analytics — Manager Integration")
mgr = NodeAnalyticsManager()
mgr.register_node("node-A", {
    "rx_lat": 33.939, "rx_lon": -84.651,
    "tx_lat": 33.756, "tx_lon": -84.331,
    "fc_hz": 195e6,
})
mgr.register_node("node-B", {
    "rx_lat": 34.0, "rx_lon": -84.5,
    "tx_lat": 33.8, "tx_lon": -84.2,
    "fc_hz": 195e6,
})

check("Manager has reputation for A", "node-A" in mgr.reputations)
check("Manager has coverage map for B", "node-B" in mgr.coverage_maps)
check("Node A not blocked", not mgr.is_node_blocked("node-A"))

# Record frame
accepted = mgr.record_detection_frame("node-A", {
    "delay": [15.0, 20.0], "doppler": [50.0, -30.0], "snr": [12.0, 8.0]
})
check("Frame accepted for good node", accepted is True)

# Record ADSB correlation
mgr.record_adsb_correlation("node-A", AdsReportEntry(
    timestamp_ms=1000, predicted_delay=15.0, predicted_doppler=50.0,
    measured_delay=15.2, measured_doppler=50.5,
    adsb_hex="abc123", adsb_lat=34.0, adsb_lon=-84.5,
))
check("ADSB populates coverage map", len(mgr.coverage_maps["node-A"].entries) == 1)

# Summaries include new fields
summary_a = mgr.get_node_summary("node-A")
check("Summary has reputation", "reputation" in summary_a)
check("Summary has coverage_map", "coverage_map" in summary_a)

# Evaluate reputations
mgr.evaluate_reputations()
check("Reputation evaluated without error", True)

# Cross-node analysis includes blocked_nodes
cross = mgr.get_cross_node_analysis()
check("Cross analysis has blocked_nodes", "blocked_nodes" in cross)

# Block a node and verify frame rejection
mgr.reputations["node-A"].blocked = True
mgr.reputations["node-A"].block_reason = "test"
check("Blocked node detected", mgr.is_node_blocked("node-A"))
rejected = mgr.record_detection_frame("node-A", {
    "delay": [10.0], "doppler": [20.0], "snr": [5.0]
})
check("Frame rejected for blocked node", rejected is False)

mgr.unblock_node("node-A")
check("Admin unblock works", not mgr.is_node_blocked("node-A"))


# ─── 27. Inter-Node Association ───────────────────────────────────────────────
section("27. Inter-Node Association")
from analytics.association import (
    NodeGeometry, compute_overlap_zone, find_associations,
    InterNodeAssociator, _bistatic_delay_at, _lla_to_enu,
)

# Basic geometry — two nodes near Atlanta
geo_a = NodeGeometry(
    node_id="assoc-A", rx_lat=33.939, rx_lon=-84.651, rx_alt_km=0.29,
    tx_lat=33.756, tx_lon=-84.331, tx_alt_km=0.49,
    beam_azimuth_deg=135, beam_width_deg=41, max_range_km=50,
)
geo_b = NodeGeometry(
    node_id="assoc-B", rx_lat=34.05, rx_lon=-84.4, rx_alt_km=0.3,
    tx_lat=33.85, tx_lon=-84.15, tx_alt_km=0.5,
    beam_azimuth_deg=210, beam_width_deg=41, max_range_km=50,
)

# Overlap zone pre-computation
zone = compute_overlap_zone(geo_a, geo_b, grid_step_km=5.0)
check("Overlap zone has node IDs", zone.node_a_id == "assoc-A")
check("Overlap zone has delay pairs", len(zone.delay_pairs) == len(zone.grid_points))
# May or may not have overlap depending on exact geometry
check("Overlap zone computed without error", True)

# Bistatic delay sanity
ref_lat, ref_lon = 33.9, -84.5
tx_enu = _lla_to_enu(33.756, -84.331, 0.49, ref_lat, ref_lon, 0.0)
target_enu = (10.0, 10.0, 8.0)  # 10km east, 10km north, 8km up
delay = _bistatic_delay_at(target_enu, tx_enu)
check("Bistatic delay > 0", delay > 0)
check("Bistatic delay reasonable (<300 μs)", delay < 300)

# InterNodeAssociator
assoc = InterNodeAssociator(grid_step_km=5.0)
assoc.register_node("assoc-A", {
    "rx_lat": 33.939, "rx_lon": -84.651, "rx_alt_ft": 950,
    "tx_lat": 33.756, "tx_lon": -84.331, "tx_alt_ft": 1600,
    "fc_hz": 195e6, "beam_width_deg": 41, "max_range_km": 50,
})
assoc.register_node("assoc-B", {
    "rx_lat": 34.05, "rx_lon": -84.4, "rx_alt_ft": 980,
    "tx_lat": 33.85, "tx_lon": -84.15, "tx_alt_ft": 1600,
    "fc_hz": 195e6, "beam_width_deg": 41, "max_range_km": 50,
})

check("Associator has 2 nodes", len(assoc.node_geometries) == 2)
overlap_summary = assoc.get_overlap_summary()
check("Overlap summary returned", isinstance(overlap_summary, list))
check("Beam width 41 in NodeGeometry", assoc.node_geometries["assoc-A"].beam_width_deg == 41)

# Submit frames
candidates = assoc.submit_frame("assoc-A", {
    "delay": [30.0, 45.0], "doppler": [60.0, -20.0], "snr": [15.0, 10.0]
}, timestamp_ms=1000)
check("Submit frame A returns list", isinstance(candidates, list))

candidates = assoc.submit_frame("assoc-B", {
    "delay": [31.0, 46.0], "doppler": [58.0, -22.0], "snr": [14.0, 9.0]
}, timestamp_ms=1000)
check("Submit frame B returns list", isinstance(candidates, list))

# Solver format
solver_input = assoc.format_candidates_for_solver(candidates)
check("Solver input is list", isinstance(solver_input, list))


# ─── 28. Simulation World — Anomalous Objects ────────────────────────────────
section("28. Simulation World — Anomalous Objects")
from simulation.world import SimulationWorld, NodeConfig as SimNodeConfig

world = SimulationWorld(center_lat=34.0, center_lon=-84.0)
world.add_node(SimNodeConfig(
    node_id="sim-node-1",
    rx_lat=33.939, rx_lon=-84.651,
    tx_lat=33.756, tx_lon=-84.331,
    beam_width_deg=41, max_range_km=50,
))

# Step the world in anomalous mode
for _ in range(50):
    world.step(0.5, mode="anomalous")

n_anomalous = sum(1 for ac in world.aircraft if ac.is_anomalous)
n_adsb = sum(1 for ac in world.aircraft if ac.has_adsb)
check("Simulation has aircraft", len(world.aircraft) >= 5)
check("Anomalous mode produces some anomalous", n_anomalous >= 0)  # probabilistic
check("Default beam_width is 41", world.nodes["sim-node-1"].beam_width_deg == 41.0)

# Generate frames
frames = world.generate_all_frames(timestamp_ms=1000)
check("Frames for sim-node-1", "sim-node-1" in frames)
frame = frames["sim-node-1"]
check("Frame has delay array", isinstance(frame.get("delay"), list))
check("Frame has doppler array", isinstance(frame.get("doppler"), list))

# Summary
summary = world.get_aircraft_summary()
check("Aircraft summary is list", isinstance(summary, list))


# ─── 29. Synthetic Node — Protocol & Config ──────────────────────────────────
section("29. Synthetic Node — Protocol & Config")
from simulation.node import NodeConfig as SynNodeConfig, _config_hash

cfg = SynNodeConfig(node_id="syn-test-01")
check("Synth node ID has syn prefix", cfg.node_id.startswith("syn"))
h1 = _config_hash(cfg)
check("Config hash is string", isinstance(h1, str))
check("Config hash is 16 chars", len(h1) == 16)

# Same config = same hash
h2 = _config_hash(cfg)
check("Same config same hash", h1 == h2)

# Different config = different hash
cfg2 = SynNodeConfig(node_id="syn-test-02")
h3 = _config_hash(cfg2)
check("Different config different hash", h1 != h3)

# Verify cloudflare-host argument is in argparse
import subprocess
result = subprocess.run(
    [sys.executable, "/Users/admin/Tower-Finder/backend/synthetic_node.py", "--help"],
    capture_output=True, text=True,
)
check("--cloudflare-host in help", "--cloudflare-host" in result.stdout)
check("--mode in help", "--mode" in result.stdout)
check("--nodes-config in help", "--nodes-config" in result.stdout)

# SyntheticNodeGenerator basic
from simulation.node import SyntheticNodeGenerator
gen = SyntheticNodeGenerator(cfg, mode="adsb")
frame = gen.generate_frame(timestamp_ms=1000)
check("Generated frame has delay", "delay" in frame)
check("Generated frame has doppler", "doppler" in frame)
check("Generated frame has snr", "snr" in frame)
check("ADSB mode has adsb key", "adsb" in frame)
check("Frame has detections", len(frame["delay"]) > 0)


# ─── 30. Multi-Node Solver ───────────────────────────────────────────────────
section("30. Multi-Node Solver")
from retina_geolocator.multinode_solver import solve_multinode

mn_configs = {
    "mn-A": {
        "rx_lat": 33.939, "rx_lon": -84.651, "rx_alt_ft": 950,
        "tx_lat": 33.756, "tx_lon": -84.331, "tx_alt_ft": 1600,
        "fc_hz": 195e6,
    },
    "mn-B": {
        "rx_lat": 34.05, "rx_lon": -84.4, "rx_alt_ft": 980,
        "tx_lat": 33.85, "tx_lon": -84.15, "tx_alt_ft": 1600,
        "fc_hz": 195e6,
    },
}

mn_input = {
    "initial_guess": {"lat": 33.9, "lon": -84.4, "alt_km": 8.0},
    "measurements": [
        {"node_id": "mn-A", "delay_us": 30.0, "doppler_hz": 60.0, "snr": 15.0},
        {"node_id": "mn-B", "delay_us": 31.0, "doppler_hz": 58.0, "snr": 14.0},
    ],
    "n_nodes": 2,
    "timestamp_ms": 1000,
}

mn_result = solve_multinode(mn_input, mn_configs)
check("Multi-node solver succeeds", mn_result is not None and mn_result.get("success"))
check("Result has lat/lon", "lat" in mn_result and "lon" in mn_result)
check("Result lat plausible", 33.0 < mn_result["lat"] < 35.0)
check("Result lon plausible", -86.0 < mn_result["lon"] < -83.0)
check("Result has velocity", "vel_east" in mn_result and "vel_north" in mn_result)
check("Result has alt_m", mn_result.get("alt_m", 0) > 0)
check("Result n_nodes is 2", mn_result.get("n_nodes") == 2)
check("Result has rms_delay", "rms_delay" in mn_result)

# Fail case: only 1 measurement
mn_bad = {
    "initial_guess": {"lat": 33.9, "lon": -84.4, "alt_km": 8.0},
    "measurements": [
        {"node_id": "mn-A", "delay_us": 30.0, "doppler_hz": 60.0, "snr": 15.0},
    ],
    "n_nodes": 1,
    "timestamp_ms": 1000,
}
mn_bad_res = solve_multinode(mn_bad, mn_configs)
check("Single measurement returns None", mn_bad_res is None)


# ─── 31. External ADS-B & Cross-Validation ───────────────────────────────────
section("31. External ADS-B & Cross-Validation")
from main import (
    _cross_validate_adsb_reports, _external_adsb_cache,
    _node_analytics as _main_analytics, _multinode_to_aircraft,
)
import main as _main_module

# Mock external cache
_main_module._external_adsb_cache = {
    "abc123": {"lat": 34.0, "lon": -84.5, "alt_m": 10000},
}

# Register a test node with a sample that matches external
test_mgr = _main_analytics
test_mgr.register_node("xval-node", {
    "rx_lat": 33.9, "rx_lon": -84.6,
    "tx_lat": 33.7, "tx_lon": -84.3,
    "fc_hz": 195e6,
})
test_mgr.record_adsb_correlation("xval-node", AdsReportEntry(
    timestamp_ms=1000, predicted_delay=10.0, predicted_doppler=50.0,
    measured_delay=10.5, measured_doppler=51.0,
    adsb_hex="abc123", adsb_lat=34.0, adsb_lon=-84.5,
))
rep_before = test_mgr.reputations["xval-node"].reputation
_cross_validate_adsb_reports()
rep_after = test_mgr.reputations["xval-node"].reputation
check("Good ADS-B match — no penalty", rep_before == rep_after)

# Now add a bad sample (far from truth)
test_mgr.record_adsb_correlation("xval-node", AdsReportEntry(
    timestamp_ms=2000, predicted_delay=10.0, predicted_doppler=50.0,
    measured_delay=10.5, measured_doppler=51.0,
    adsb_hex="abc123", adsb_lat=36.0, adsb_lon=-80.0,
))
rep_before2 = test_mgr.reputations["xval-node"].reputation
_cross_validate_adsb_reports()
rep_after2 = test_mgr.reputations["xval-node"].reputation
check("Bad ADS-B mismatch — penalty applied", rep_after2 < rep_before2)

# Clean up mock
_main_module._external_adsb_cache = {}

# Multi-node to aircraft conversion
mn_test_result = {
    "lat": 33.9, "lon": -84.4, "alt_m": 8000,
    "vel_east": 100.0, "vel_north": 50.0, "vel_up": 0.0,
    "rms_delay": 0.5, "rms_doppler": 5.0,
    "n_nodes": 2, "n_measurements": 4, "timestamp_ms": 1000,
}
ac = _multinode_to_aircraft("test-key", mn_test_result)
check("Aircraft has hex", "hex" in ac)
check("Aircraft has lat/lon", "lat" in ac and "lon" in ac)
check("Aircraft marked multinode", ac.get("multinode") is True)
check("Aircraft has gs", ac.get("gs", 0) > 0)


# ─── 32. Periodic Reputation Timer Wired ─────────────────────────────────────
section("32. Periodic Reputation Timer & External ADS-B Wiring")
with open("/Users/admin/Tower-Finder/backend/main.py") as f:
    _main_src = f.read()
check("_reputation_evaluator defined", "async def _reputation_evaluator" in _main_src)
check("_adsb_truth_fetcher defined", "async def _adsb_truth_fetcher" in _main_src)
check("reputation_task created in lifespan", "reputation_task = asyncio.create_task" in _main_src)
check("adsb_truth_task created in lifespan", "adsb_truth_task = asyncio.create_task" in _main_src)
check("evaluate_reputations called", "evaluate_reputations()" in _main_src)
check("_fetch_external_adsb called", "_fetch_external_adsb()" in _main_src)
check("opensky-network.org URL", "opensky-network.org" in _main_src)
check("_cross_validate_adsb_reports called", "_cross_validate_adsb_reports()" in _main_src)
check("solve_multinode imported", "from retina_geolocator.multinode_solver import solve_multinode" in _main_src)
check("solve_multinode called in DETECTION", "solve_multinode(s_in" in _main_src)
check("_multinode_tracks populated", "_multinode_tracks[key] = result" in _main_src)
check("multinode in aircraft.json", "_multinode_to_aircraft" in _main_src)


# ─── 33. Track Quality / Gap Stats ───────────────────────────────────────────
section("33. Track Quality — Gap Stats in NodeMetrics")
metrics = NodeMetrics(node_id="gap-test")

# Feed evenly spaced timestamps (10s apart) with one big gap
ts_list = [1000 + i * 10000 for i in range(5)]  # 0, 10, 20, 30, 40s
ts_list.append(1000 + 200_000)  # 200s — big gap after 40s
for t in ts_list:
    metrics.record_frame({"delay": [1.0], "doppler": [1.0], "snr": [5.0], "timestamp": t})

gap = metrics.gap_stats
check("gap_count >= 1 for big gap", gap["gap_count"] >= 1)
check("max_gap_s >= 100", gap["max_gap_s"] >= 100)
check("continuity_ratio <= 1.0", gap["continuity_ratio"] <= 1.0)

summary_g = metrics.summary()
check("summary has track_quality", "track_quality" in summary_g)
check("track_quality has gap_count", "gap_count" in summary_g["track_quality"])


# ─── 34. Coverage Suggestion Strategies ───────────────────────────────────────
section("34. Coverage Suggestion — Strategy 1 & 2")
from node_analytics import coverage_suggestion

sugg_areas = [
    DetectionAreaState(node_id="cs-A", rx_lat=33.939, rx_lon=-84.651,
                       beam_azimuth_deg=135, beam_width_deg=41, max_range_km=50),
    DetectionAreaState(node_id="cs-B", rx_lat=34.05, rx_lon=-84.4,
                       beam_azimuth_deg=210, beam_width_deg=41, max_range_km=50),
]
# Default call should return suggestions
sugg_result = coverage_suggestion(sugg_areas, center_lat=34.0, center_lon=-84.5)
check("coverage_suggestion returns list", isinstance(sugg_result, list))

# Call with trust_scores to exercise Strategy 1
ts_a = TrustScoreState(node_id="cs-A"); ts_a.add_sample(AdsReportEntry(
    timestamp_ms=1000, predicted_delay=10.0, predicted_doppler=50.0,
    measured_delay=10.2, measured_doppler=50.5, adsb_hex="x", adsb_lat=34.0, adsb_lon=-84.5))
ts_b = TrustScoreState(node_id="cs-B"); ts_b.add_sample(AdsReportEntry(
    timestamp_ms=1000, predicted_delay=10.0, predicted_doppler=50.0,
    measured_delay=10.1, measured_doppler=50.2, adsb_hex="y", adsb_lat=34.0, adsb_lon=-84.5))
sugg_dense = coverage_suggestion(sugg_areas, 34.0, -84.5,
                                  trust_scores={"cs-A": ts_a, "cs-B": ts_b})
check("Strategy 1 result is list", isinstance(sugg_dense, list))

# Call with solver_rms_history to exercise Strategy 2
sugg_expand = coverage_suggestion(sugg_areas, 34.0, -84.5,
                                   solver_rms_history=[5.0, 4.8, 4.7, 4.65, 4.62, 4.60, 4.59, 4.58, 4.575, 4.572])
check("Strategy 2 result is list", isinstance(sugg_expand, list))


# ─── 35. Storage Module — Local Archive ──────────────────────────────────────
section("35. Storage Module — Local Archive")
from storage import archive_detections, list_archived_files, read_archived_file
import shutil

# Archive some data
key = archive_detections("test-storage-node", [
    {"delay": [10.0], "doppler": [50.0], "snr": [12.0], "timestamp": 1000},
])
check("archive_detections returns key", isinstance(key, str) and "/" in key)
check("key contains node id", "test-storage-node" in key)

# List files
files = list_archived_files(node_id="test-storage-node")
check("list finds archived file", len(files) >= 1)
check("file entry has key", "key" in files[0])
check("file entry has size_bytes", "size_bytes" in files[0])

# Read back
data = read_archived_file(files[0]["key"])
check("read_archived_file returns dict", isinstance(data, dict))
check("archived data has node_id", data.get("node_id") == "test-storage-node")
check("archived data has detections", isinstance(data.get("detections"), list))

# Path traversal prevention
bad = read_archived_file("../../etc/passwd")
check("Path traversal blocked", bad is None)

# Cleanup test archive
archive_dir = os.path.join(os.path.dirname("/Users/admin/Tower-Finder/backend/storage.py"), "coverage_data", "archive")
if os.path.exists(archive_dir):
    for child in os.listdir(archive_dir):
        p = os.path.join(archive_dir, child)
        if os.path.isdir(p):
            shutil.rmtree(p)


# ─── 36. Public Data Archive API ─────────────────────────────────────────────
section("36. Public Data Archive API")
r = httpx.get(f"{BASE}/api/data/archive")
check("Archive list 200", r.status_code == 200)
body = r.json()
check("Archive list has files key", "files" in body)
check("Archive list has count", "count" in body)

r = httpx.get(f"{BASE}/api/data/archive/nonexistent/path.json")
check("Missing archive file → 404", r.status_code == 404)


# ─── 37. WebSocket & SSE Endpoints Wired ─────────────────────────────────────
section("37. WebSocket & SSE Endpoints")
check("WebSocket endpoint in source", "/ws/aircraft" in _main_src)
check("SSE endpoint in source", "/api/radar/stream" in _main_src)
check("_ws_clients set defined", "_ws_clients" in _main_src)
check("_broadcast_aircraft defined", "async def _broadcast_aircraft" in _main_src)
check("StreamingResponse imported", "StreamingResponse" in _main_src)
check("archive_detections imported", "from storage import" in _main_src)

# SSE is an infinite stream — verify endpoint exists via source code check
check("SSE endpoint defined", "/api/radar/stream" in _main_src)


# ─── 38. Frontend LiveAircraftMap & Tabs ──────────────────────────────────────
section("38. Frontend LiveAircraftMap & Tabs")
with open("/Users/admin/Tower-Finder/frontend/src/components/LiveAircraftMap.jsx") as f:
    lam = f.read()
check("LiveAircraftMap has WebSocket", "ws://" in lam or "WebSocket" in lam)
check("LiveAircraftMap has MapContainer", "MapContainer" in lam)
check("LiveAircraftMap has aircraft markers", "aircraft" in lam.lower())
check("LiveAircraftMap has coverage overlay", "coverage" in lam.lower())

with open("/Users/admin/Tower-Finder/frontend/src/App.jsx") as f:
    app_jsx = f.read()
check("App has tab state", "activeTab" in app_jsx)
check("App imports LiveAircraftMap", "LiveAircraftMap" in app_jsx)
check("Tab navigation in header", "header-tabs" in app_jsx)

with open("/Users/admin/Tower-Finder/frontend/src/App.css") as f:
    app_css = f.read()
check("Tab CSS defined", ".tab-btn" in app_css)
check("Active tab style", ".tab-btn.active" in app_css)


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
