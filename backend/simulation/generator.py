"""
Fleet Generator — Generates 100-1000 realistic synthetic node configurations
spread across multiple geographic regions, each paired with a real broadcast
transmitter tower.

Usage:
    python fleet_generator.py --nodes 200 --output fleet_config.json
    python fleet_generator.py --nodes 1000 --regions us,eu,au
"""

import argparse
import json
import math
import random
import sys
from dataclasses import dataclass, asdict
from typing import Optional

# ── Broadcast tower databases by region ──────────────────────────────────────
# Each tower: (lat, lon, alt_ft, freq_hz, callsign)
# Modeled after real VHF/UHF broadcast transmitters suitable for passive radar

_TOWERS_US = [
    # East Coast
    (33.75667, -84.33184, 1600, 195_000_000, "WSB-TV"),    # Atlanta
    (35.23064, -80.84313, 1540, 575_000_000, "WBTV"),       # Charlotte
    (38.93460, -77.07920, 1380, 585_000_000, "WRC-TV"),     # Washington DC
    (40.74843, -73.98566, 1776, 191_000_000, "WCBS-TV"),    # New York
    (42.35370, -71.06010, 1200, 575_000_000, "WBZ-TV"),     # Boston
    (39.95233, -75.16379, 1600, 563_000_000, "KYW-TV"),     # Philadelphia
    (25.79590, -80.28700, 1000, 191_000_000, "WTVJ"),       # Miami
    (28.54082, -81.37916, 1350, 551_000_000, "WESH"),       # Orlando
    (27.97450, -82.45720, 1400, 539_000_000, "WFLA"),       # Tampa
    (30.33270, -81.65560, 1200, 575_000_000, "WJXT"),       # Jacksonville
    (36.85260, -75.97820, 1300, 539_000_000, "WAVY"),       # Norfolk
    (35.78700, -78.78170, 1500, 563_000_000, "WRAL"),       # Raleigh
    # Midwest
    (41.87150, -87.62440, 1650, 191_000_000, "WBBM-TV"),   # Chicago
    (42.33140, -83.04580, 1200, 551_000_000, "WXYZ-TV"),   # Detroit
    (39.96110, -82.99880, 1400, 563_000_000, "WCMH"),      # Columbus
    (39.76910, -86.15800, 1350, 575_000_000, "WISH-TV"),   # Indianapolis
    (44.97750, -93.26490, 1500, 585_000_000, "WCCO-TV"),   # Minneapolis
    (38.62720, -90.19780, 1300, 551_000_000, "KMOV"),       # St Louis
    (39.09970, -94.57860, 1450, 539_000_000, "KCTV"),       # Kansas City
    (41.25220, -95.99780, 1350, 575_000_000, "KETV"),       # Omaha
    # South
    (29.76330, -95.36320, 1300, 191_000_000, "KHOU"),       # Houston
    (32.78060, -96.80060, 1600, 575_000_000, "WFAA"),       # Dallas
    (29.42410, -98.49360, 1200, 563_000_000, "KENS"),       # San Antonio
    (30.26710, -97.74310, 1400, 551_000_000, "KVUE"),       # Austin
    (36.16270, -86.78160, 1350, 539_000_000, "WSMV"),       # Nashville
    (35.14950, -90.04890, 1200, 575_000_000, "WMC-TV"),    # Memphis
    (32.29560, -90.18480, 1100, 563_000_000, "WLBT"),      # Jackson MS
    (30.45080, -91.18720, 1150, 551_000_000, "WAFB"),      # Baton Rouge
    # West
    (34.05220, -118.24370, 1600, 191_000_000, "KABC-TV"),  # Los Angeles
    (37.77490, -122.41940, 1500, 575_000_000, "KGO-TV"),   # San Francisco
    (47.60620, -122.33210, 1400, 585_000_000, "KOMO-TV"),  # Seattle
    (45.52350, -122.67620, 1300, 551_000_000, "KGW"),       # Portland
    (33.44840, -112.07400, 1200, 563_000_000, "KPHO-TV"),  # Phoenix
    (36.17490, -115.13740, 1150, 539_000_000, "KLAS-TV"),  # Las Vegas
    (39.73920, -104.99030, 1400, 575_000_000, "KCNC-TV"),  # Denver
    (40.76080, -111.89100, 1300, 563_000_000, "KSL-TV"),   # Salt Lake City
    (32.71570, -117.16110, 1100, 551_000_000, "KFMB-TV"),  # San Diego
    (36.74770, -119.77260, 1200, 539_000_000, "KFSN-TV"),  # Fresno
]

_TOWERS_EU = [
    # UK
    (51.50740, -0.12780, 1000, 690_000_000, "Crystal Palace"),
    (53.47620, -2.22320, 1060, 706_000_000, "Winter Hill"),
    (52.68660, -2.43120, 1140, 730_000_000, "Sutton Coldfield"),
    (55.95320, -3.18830, 980, 674_000_000, "Black Hill"),
    (51.45150, -2.59270, 920, 698_000_000, "Mendip"),
    # Germany
    (52.52060, 13.40490, 1200, 482_000_000, "Berlin Alexanderplatz"),
    (48.13510, 11.58200, 960, 514_000_000, "München Olympiaturm"),
    (50.11090, 8.68210, 1100, 498_000_000, "Frankfurt Europaturm"),
    (53.55110, 9.99370, 1040, 530_000_000, "Hamburg Heinrich-Hertz"),
    (51.22770, 6.77350, 980, 546_000_000, "Düsseldorf Rheinturm"),
    # France
    (48.85830, 2.29450, 1050, 474_000_000, "Tour Eiffel"),
    (43.29650, 5.36980, 900, 490_000_000, "Marseille Grande Étoile"),
    (45.76400, 4.83570, 950, 506_000_000, "Lyon Fourvière"),
    (44.83780, -0.57950, 880, 522_000_000, "Bordeaux Bouliac"),
    # Spain / Italy
    (40.41680, -3.70380, 950, 538_000_000, "Madrid Torrespaña"),
    (41.38790, 2.16990, 1000, 554_000_000, "Barcelona Collserola"),
    (41.90270, 12.49630, 920, 570_000_000, "Roma Monte Mario"),
    (45.46430, 9.18950, 980, 586_000_000, "Milano Valassina"),
    # Scandinavia
    (59.33260, 18.06490, 1100, 602_000_000, "Stockholm Kaknäs"),
    (60.17100, 24.93750, 860, 618_000_000, "Helsinki Pasila"),
]

_TOWERS_AU = [
    (33.86880, 151.20930, 1200, 226_500_000, "Sydney TCN-9"),
    (-37.81360, 144.96310, 1100, 182_250_000, "Melbourne GTV-9"),
    (-27.46980, 153.02510, 1000, 191_625_000, "Brisbane QTQ-9"),
    (-34.92850, 138.60070, 950, 209_250_000, "Adelaide NWS-9"),
    (-31.95050, 115.86040, 900, 196_250_000, "Perth STW-9"),
    (-42.88260, 147.32710, 850, 182_250_000, "Hobart TVT-6"),
    (-35.28100, 149.13000, 920, 226_500_000, "Canberra CTC-10"),
    (-19.25900, 146.81690, 800, 196_250_000, "Townsville TNQ-7"),
    (-12.46340, 130.84560, 780, 191_625_000, "Darwin DTQ-8"),
    (-33.42990, 149.10050, 860, 209_250_000, "Orange CBN-8"),
]

# ── Rural / isolated towers for "solo node" testing ──────────────────────────
# These are real US broadcast transmitters in areas with no nearby metro clusters.
# Each is at least ~150 km from the nearest _TOWERS_US entry, so nodes here
# can never share coverage with a metro node and will always produce solo arcs.
_TOWERS_SOLO_US = [
    # Great Plains / High Plains
    (44.07500, -103.22830, 1100, 551_000_000, "KEVN-Rapid City"),     # SD
    (46.87190, -113.99300, 1050, 563_000_000, "KPAX-Missoula"),       # MT
    (43.03540, -108.05270, 1000, 539_000_000, "KCWY-Casper"),         # WY
    (48.23000, -101.29600,  980, 575_000_000, "KMOT-Minot"),          # ND
    (38.81130,  -99.32640,  960, 551_000_000, "KAYS-Hays"),           # KS central
    (32.44180, -104.22840,  900, 563_000_000, "KCAV-Carlsbad"),       # NM SE
    # Desert Southwest
    (35.19900, -111.65100, 1200, 563_000_000, "KNAZ-Flagstaff"),      # AZ
    (40.83870, -115.76270,  920, 539_000_000, "KELK-Elko"),           # NV
    (31.87220, -106.42920,  880, 539_000_000, "KTSM-El Paso"),        # TX border
    (36.90000, -111.50000, 1100, 539_000_000, "KPGE-Page-AZ"),        # AZ NE
    (39.50000, -119.80000,  950, 551_000_000, "KRNV-Reno"),           # NV
    # South / Central
    (34.74020,  -92.28990,  920, 563_000_000, "KATV-Little Rock"),    # AR
    (37.68610,  -97.33010,  940, 551_000_000, "KWCH-Wichita"),        # KS
    # Pacific NW interior
    (42.55000, -114.46000,  980, 575_000_000, "KXTF-Twin Falls"),     # ID
    # Rural Midwest / Great Lakes
    (46.48730,  -84.35670,  900, 563_000_000, "KBSF-Sault Ste Marie"),# MI UP
    (46.78650,  -92.10350,  940, 551_000_000, "KDLH-Duluth"),         # MN
    (47.92500,  -97.03260,  920, 563_000_000, "WDAY-Fargo"),          # ND
    (43.54960,  -96.72960,  930, 539_000_000, "KSFY-Sioux Falls"),    # SD
    (46.37000,  -94.87000,  920, 539_000_000, "KBRJ-Brainerd"),       # MN lakes
    (45.00000,  -85.50000,  880, 563_000_000, "WPBN-Traverse"),       # MI north
    # East
    (44.06000,  -76.15000,  850, 563_000_000, "WWTI-Watertown"),      # NY
    (37.30000,  -79.50000,  880, 563_000_000, "WSLS-Roanoke"),        # VA
]


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return great-circle distance in km between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


# Rural US bounding boxes used when the named solo pool needs extending.
# Each box: (lat_min, lat_max, lon_min, lon_max)
_RURAL_BOXES_US = [
    (38.0, 49.0, -116.0, -96.0),   # Northern Great Plains (MT/ND/SD/NE/WY)
    (32.0, 42.0, -108.0, -96.0),   # Southern Great Plains (KS/OK/TX panhandle/NM)
    (32.0, 42.0, -117.0, -108.0),  # Desert Southwest (AZ/NV/UT/western NM)
    (40.0, 50.0, -122.0, -111.0),  # Pacific NW interior (eastern OR/WA/ID)
    (35.0, 46.0, -111.0, -103.0),  # Rockies (CO/WY/MT eastern slope)
    (38.0, 47.0, -99.0,  -88.0),   # Rural Midwest (IA/MN/WI/IL away from cities)
]
_RURAL_FREQS = [539_000_000, 551_000_000, 563_000_000, 575_000_000, 585_000_000]


def _extend_solo_pool(
    current_pool: list,
    n_needed: int,
    avoid_positions: list[tuple[float, float]],
    min_sep_km: float = 180.0,
    max_attempts: int = 200_000,
) -> list:
    """Extend *current_pool* to at least *n_needed* entries.

    Named towers in *current_pool* are gated with the same *min_sep_km* check
    against *avoid_positions* and each other before being accepted.  New
    synthetic towers are then placed in rural US bounding boxes via
    rejection-sampling so every accepted position is at least *min_sep_km*
    from all others.
    Returns the (possibly extended) pool.
    """
    pool = []
    busy = list(avoid_positions)          # positions already taken

    # Gate named entries — apply the same minimum-separation rule so named
    # towers that are too close to each other or to metro positions are skipped.
    for item in current_pool:
        lat, lon = item[0], item[1]
        if not any(_haversine_km(lat, lon, p[0], p[1]) < min_sep_km for p in busy):
            pool.append(item)
            busy.append((lat, lon))

    attempts = 0
    while len(pool) < n_needed and attempts < max_attempts:
        box = random.choice(_RURAL_BOXES_US)
        lat = random.uniform(box[0], box[1])
        lon = random.uniform(box[2], box[3])

        # Reject if too close to any known position
        if any(_haversine_km(lat, lon, p[0], p[1]) < min_sep_km for p in busy):
            attempts += 1
            continue

        alt_ft = random.uniform(800, 1400)
        fc = random.choice(_RURAL_FREQS)
        callsign = f"RURAL-{len(pool) + 1:04d}"
        pool.append((lat, lon, alt_ft, fc, callsign))
        busy.append((lat, lon))
        attempts += 1

    return pool


@dataclass
class GeneratedNodeConfig:
    """A generated node in the fleet."""
    node_id: str
    rx_lat: float
    rx_lon: float
    rx_alt_ft: float
    tx_lat: float
    tx_lon: float
    tx_alt_ft: float
    fc_hz: float
    fs_hz: float = 2_000_000.0
    beam_width_deg: float = 40.0
    max_range_km: float = 50.0
    region: str = "us"
    tx_callsign: str = ""


def _jitter(val: float, sigma: float) -> float:
    """Add Gaussian jitter to a value."""
    return val + random.gauss(0, sigma)


# ── Water / ocean rejection ──────────────────────────────────────────────────
# Simple bounding-box check for known US water bodies.  Positions inside any
# of these boxes are rejected so synthetic receiver sites don't end up in the
# ocean, Great Lakes, or large bays.

_WATER_BOXES: list[tuple[float, float, float, float]] = [
    # (lat_min, lat_max, lon_min, lon_max)
    # Great Lakes
    (41.5, 49.0, -92.5, -76.0),   # approximate Great Lakes bounding box
    # Gulf of Mexico (open water — broad nearshore strip)
    (18.0, 30.5, -98.0, -80.0),
    # Atlantic east of Florida / Florida Straits / Bahamas
    # Closes the -80W→-72W gap that makes nodes appear in the ocean near Miami
    (24.0, 31.0, -81.0, -72.0),
    # Atlantic Ocean — open ocean east of the eastern seaboard
    (24.0, 47.5, -72.0, -60.0),
    # Pacific Ocean — truly offshore strip (cities are east of -117°W)
    (32.0, 49.0, -130.0, -125.0),
    # Tampa Bay (Gulf box doesn't have enough land-point density to cover the ~30km width)
    (27.35, 28.1, -82.85, -82.2),
    # Charlotte Harbor / Pine Island Sound FL
    (26.5, 27.1, -82.35, -81.85),
    # Sarasota Bay / Little Sarasota Bay FL
    (27.1, 27.55, -82.75, -82.5),
    # Lake Pontchartrain LA (35km wide — nearby land points can't bridge it)
    (30.05, 30.45, -90.55, -89.65),
    # Corpus Christi Bay TX
    (27.7, 27.95, -97.5, -97.05),
    # Matagorda Bay TX
    (28.45, 28.8, -96.75, -96.15),
    # Pamlico Sound / Albemarle Sound NC
    (35.0, 36.1, -76.85, -75.65),
    # Chesapeake Bay
    (36.8, 39.5, -76.5, -75.8),
    # Puget Sound
    (47.0, 48.8, -123.5, -122.0),
    # San Francisco Bay
    (37.4, 38.2, -122.5, -121.9),
    # Long Island Sound
    (40.8, 41.4, -73.8, -71.8),
    # Delaware Bay
    (38.7, 39.6, -75.6, -74.9),
    # Mobile Bay / Pensacola Bay
    (30.0, 30.8, -88.2, -87.3),
    # Galveston Bay / Houston Ship Channel
    (29.3, 29.9, -95.1, -94.4),
]

# Known land points near coasts — locations within the water bounding boxes
# that are actually on land (e.g. cities on Great Lakes shores).
# If an RX position is within 5 km of a land point, it's allowed.
_COASTAL_LAND_POINTS: list[tuple[float, float]] = [
    # Great Lakes cities
    (41.88, -87.63),   # Chicago
    (42.33, -83.05),   # Detroit
    (41.50, -81.69),   # Cleveland
    (43.16, -79.24),   # Niagara Falls
    (42.89, -78.88),   # Buffalo
    (44.98, -93.27),   # Minneapolis
    (43.04, -87.91),   # Milwaukee
    (42.96, -85.67),   # Grand Rapids
    (46.79, -92.10),   # Duluth
    (43.05, -89.40),   # Madison WI
    (44.51, -88.01),   # Green Bay WI
    (42.96, -82.45),   # Port Huron MI
    (44.27, -85.60),   # Cadillac MI
    (42.26, -85.59),   # Kalamazoo MI
    (41.66, -83.56),   # Toledo OH / Maumee Bay
    (43.96, -77.96),   # Oswego NY
    (44.70, -75.48),   # Ogdensburg NY
    (43.45, -76.51),   # Oswego / Pulaski NY
    # Gulf Coast cities
    (29.76, -95.36),   # Houston
    (30.27, -97.74),   # Austin
    (30.45, -91.19),   # Baton Rouge
    (30.00, -90.07),   # New Orleans
    (27.95, -82.46),   # Tampa
    (25.76, -80.19),   # Miami
    (28.54, -81.38),   # Orlando
    (30.33, -81.66),   # Jacksonville
    (27.77, -82.64),   # St. Petersburg
    (29.42, -98.49),   # San Antonio
    (30.39, -87.69),   # Pensacola
    (30.22, -92.02),   # Lafayette
    (29.95, -90.07),   # New Orleans Lakeshore
    (30.69, -88.04),   # Mobile AL
    (29.70, -95.01),   # Pasadena TX
    (29.55, -95.13),   # League City TX
    # Tampa Bay shores (box: 27.35-28.1°N, -82.85 to -82.4°W)
    # NOTE: only inland / peninsula cities — do NOT add right-on-shore suburbs
    # (Ruskin, Apollo Beach, Gibsonton) because at any positive radius their
    # circle extends into the bay and exempts mid-bay positions.
    (27.97, -82.80),   # Clearwater FL
    (28.02, -82.77),   # Dunedin FL
    (27.99, -82.69),   # Safety Harbor FL
    (27.94, -82.29),   # Brandon FL
    (27.87, -82.33),   # Riverview FL
    (27.52, -82.57),   # Palmetto FL
    (27.50, -82.57),   # Bradenton FL
    (27.34, -82.54),   # Sarasota FL
    # Charlotte Harbor shores (box: 26.5-27.1°N, -82.35 to -81.85°W)
    (27.09, -82.43),   # Venice FL (north edge)
    (26.93, -82.05),   # Port Charlotte FL
    (26.63, -81.87),   # Cape Coral FL (east)
    (26.71, -81.93),   # Punta Gorda FL
    # Sarasota Bay (box: 27.1-27.55°N, -82.75 to -82.5°W)
    (27.34, -82.54),   # Sarasota FL (already above, reuses)
    (27.48, -82.57),   # North Port FL
    # Lake Pontchartrain shores (box: 30.05-30.45°N, -90.55 to -89.65°W)
    (30.07, -89.93),   # Slidell LA (east shore)
    (30.43, -90.10),   # Mandeville LA (north shore)
    (30.20, -90.23),   # Metairie / Kenner LA (south shore)
    (30.18, -89.75),   # Bay St. Louis MS
    # Corpus Christi Bay (box: 27.7-27.95°N, -97.5 to -97.05°W)
    (27.80, -97.40),   # Corpus Christi TX
    (27.73, -97.14),   # Portland TX
    (27.86, -97.08),   # Ingleside TX
    # Matagorda Bay (box: 28.45-28.8°N, -96.75 to -96.15°W)
    (28.69, -96.00),   # El Campo / Bay City TX
    (28.60, -96.10),   # Palacios TX
    (28.72, -96.67),   # Bay City area
    # Pamlico / Albemarle Sound (box: 35.0-36.1°N, -76.85 to -75.65°W)
    (35.54, -77.07),   # Greenville NC (west)
    (35.10, -76.89),   # New Bern NC (southwest)
    (36.07, -76.77),   # Elizabeth City NC (north)
    (36.00, -75.68),   # Kill Devil Hills / OBX NC (east shore)
    (35.26, -75.71),   # Ocracoke Island NC (southeast)
    # Florida Atlantic coast cities (for FL Atlantic water box)
    (26.12, -80.14),   # Fort Lauderdale FL
    (26.36, -80.08),   # Boca Raton FL
    (26.72, -80.05),   # West Palm Beach FL
    (27.20, -80.25),   # Stuart / Treasure Coast FL
    (27.64, -80.40),   # Vero Beach FL
    (28.08, -80.61),   # Melbourne / Brevard County FL
    (28.45, -80.79),   # Cocoa / Rockledge FL
    (28.61, -80.82),   # Titusville / Merritt Island FL
    (29.03, -80.93),   # New Smyrna Beach FL
    (29.21, -81.00),   # Daytona Beach FL
    (29.89, -81.31),   # St. Augustine FL
    (30.28, -81.39),   # Jacksonville Beach FL
    # Eastern seaboard
    (38.91, -77.04),   # Washington DC
    (39.95, -75.16),   # Philadelphia
    (40.71, -74.01),   # New York
    (42.36, -71.06),   # Boston
    (36.85, -75.98),   # Norfolk
    (32.78, -79.93),   # Charleston SC
    (34.22, -77.91),   # Wilmington NC
    (33.45, -75.96),   # outer banks NC (off coast; excluded)
    (38.32, -75.09),   # Ocean City MD
    (39.94, -74.07),   # Toms River NJ
    (40.92, -72.64),   # Long Island NY (east)
    (41.27, -72.89),   # New Haven CT
    (41.46, -71.31),   # Providence RI
    (43.66, -70.25),   # Portland ME
    (44.80, -68.77),   # Bangor ME
    (44.42, -73.14),   # Burlington VT (Lake Champlain)
    (43.09, -76.15),   # Syracuse NY
    (42.45, -76.51),   # Ithaca NY
    (44.18, -76.49),   # Kingston ON / Wolfe Island
    # Pacific coast
    (34.05, -118.24),  # Los Angeles
    (37.77, -122.42),  # San Francisco
    (47.61, -122.33),  # Seattle
    (45.52, -122.68),  # Portland
    (33.45, -117.61),  # San Clemente
    (32.72, -117.16),  # San Diego
    (36.60, -121.89),  # Monterey CA
    (35.38, -120.85),  # San Luis Obispo CA
    (34.41, -119.69),  # Santa Barbara CA
    (33.99, -118.46),  # Santa Monica CA
    (37.97, -122.52),  # San Rafael CA
    (37.52, -122.05),  # Fremont CA
    (37.34, -121.88),  # San Jose CA
    (38.58, -121.49),  # Sacramento CA (inland, near Sacramento River delta)
    (48.52, -122.62),  # Anacortes WA
    (48.11, -122.76),  # Port Townsend WA
    (46.14, -123.83),  # Astoria OR
    (44.63, -124.06),  # Newport OR
    (42.32, -122.87),  # Medford OR
]


def _is_on_water(lat: float, lon: float) -> bool:
    """Heuristic check if a position is likely on water (ocean, lakes, bays)."""
    for lat_min, lat_max, lon_min, lon_max in _WATER_BOXES:
        if lat_min <= lat <= lat_max and lon_min <= lon <= lon_max:
            # Check if near a known coastal land point.
            # 6km radius: tight enough that shore-city circles don't bridge wide bays.
            for land_lat, land_lon in _COASTAL_LAND_POINTS:
                if _haversine_km(lat, lon, land_lat, land_lon) < 6.0:
                    return False
            return True
    return False


def _place_rx_on_land(
    tx_lat: float, tx_lon: float,
    dist_min_km: float = 5.0, dist_max_km: float = 40.0,
    max_attempts: int = 80,
) -> tuple[float, float]:
    """Place an RX position near a tower, rejecting water locations."""
    R = 6371.0
    last_rx_lat, last_rx_lon = tx_lat, tx_lon
    for _ in range(max_attempts):
        distance_km = random.uniform(dist_min_km, dist_max_km)
        bearing_rad = random.uniform(0, 2 * math.pi)
        dlat = (distance_km * math.cos(bearing_rad)) / R
        dlon = (distance_km * math.sin(bearing_rad)) / (
            R * math.cos(math.radians(tx_lat))
        )
        rx_lat = tx_lat + math.degrees(dlat)
        rx_lon = tx_lon + math.degrees(dlon)
        last_rx_lat, last_rx_lon = rx_lat, rx_lon
        if not _is_on_water(rx_lat, rx_lon):
            return (round(rx_lat, 6), round(rx_lon, 6))
    # All attempts landed in water. Walk inland from the TX in 4 cardinal
    # directions (N, E, S, W) at 5km steps up to 100km — guaranteed to find
    # land unless the TX tower itself is on a tiny offshore island.
    for step_km in range(5, 105, 5):
        for bearing_deg in (0, 90, 180, 270, 45, 135, 225, 315):
            bearing_rad = math.radians(bearing_deg)
            dlat = (step_km * math.cos(bearing_rad)) / R
            dlon = (step_km * math.sin(bearing_rad)) / (
                R * math.cos(math.radians(tx_lat))
            )
            rx_lat = tx_lat + math.degrees(dlat)
            rx_lon = tx_lon + math.degrees(dlon)
            if not _is_on_water(rx_lat, rx_lon):
                return (round(rx_lat, 6), round(rx_lon, 6))
    # Absolute last resort: return last random attempt even if on water
    return (round(last_rx_lat, 6), round(last_rx_lon, 6))


def generate_fleet(
    n_nodes: int = 200,
    regions: Optional[list[str]] = None,
    seed: int = 42,
    solo_fraction: float = 0.10,
    use_tower_api: bool = True,
) -> list[dict]:
    """Generate a fleet of synthetic node configurations.

    Nodes are distributed across the requested regions, each associated
    with a nearby broadcast tower as its illumination source. Receivers
    are placed 5-40 km from the tower (realistic for passive radar).

    When use_tower_api is True (default), each metro area queries the
    Tower Search API (towers.retina.fm) to get multiple real towers,
    so nodes in the same city use DIFFERENT towers. Falls back to the
    hardcoded tower list when the API is unreachable or returns no results.

    A fraction of nodes (solo_fraction, default 10%) are placed at isolated
    rural towers far from any other nodes, useful for testing single-node
    ellipse-arc function without overlapping detection zones.

    Args:
        n_nodes: Total nodes to generate (100-1000).
        regions: List of regions to distribute across ["us", "eu", "au"].
        seed: Random seed for reproducibility.
        solo_fraction: Fraction of nodes allocated as solo/isolated (0.0-1.0).
        use_tower_api: Query towers.retina.fm for diverse per-node towers.

    Returns:
        List of node config dicts ready for fleet_config.json.
    """
    if regions is None:
        regions = ["us"]

    random.seed(seed)

    tower_db = {
        "us": _TOWERS_US,
        "eu": _TOWERS_EU,
        "au": _TOWERS_AU,
    }

    # Solo towers — only available for US region (where rural towers are defined)
    solo_towers = _TOWERS_SOLO_US if "us" in regions else []

    # Distribute nodes across regions proportionally to tower count
    available_towers = []
    for region in regions:
        towers = tower_db.get(region, [])
        for t in towers:
            available_towers.append((region, t))

    if not available_towers:
        raise ValueError(f"No towers available for regions: {regions}")

    # ── Pre-fetch real towers from Tower API for metro areas ──────────────────
    # Each metro area gets multiple real towers so nodes in the same city
    # use DIFFERENT transmitters instead of all sharing the same one.
    metro_api_towers: dict[str, list[dict]] = {}
    _cache_key = lambda lat, lon: f"{lat:.4f},{lon:.4f}"
    if use_tower_api:
        try:
            try:
                from simulation.tower_resolver import lookup_metro_towers
            except ImportError:
                from tower_resolver import lookup_metro_towers
            all_metro_centers = []
            for region in regions:
                for t in tower_db.get(region, []):
                    all_metro_centers.append(t)
            metro_api_towers_raw = lookup_metro_towers(all_metro_centers, radius_km=80, limit=50)
            # Map back to (lat, lon) → tower list
            for t in all_metro_centers:
                key = _cache_key(t[0], t[1])
                if key in metro_api_towers_raw and metro_api_towers_raw[key]:
                    metro_api_towers[key] = metro_api_towers_raw[key]
        except Exception as exc:
            import logging
            logging.warning("Tower API lookup failed, using hardcoded towers: %s", exc)

    # Allocate solo node count
    n_solo = max(1, round(n_nodes * solo_fraction)) if solo_towers else 0
    n_metro = n_nodes - n_solo

    # Track how many times each API tower has been used (per metro) for
    # round-robin distribution — avoids all nodes sharing one tower.
    _metro_tower_idx: dict[str, int] = {}

    nodes = []
    # --- Metro nodes (clustered near metro towers) ---
    for i in range(n_metro):
        region, tower = random.choice(available_towers)
        tx_lat, tx_lon, tx_alt_ft, fc_hz, callsign = tower

        # Try to use a DIFFERENT real tower from the Tower API for this node
        key = _cache_key(tx_lat, tx_lon)
        api_towers = metro_api_towers.get(key, [])
        if api_towers:
            idx = _metro_tower_idx.get(key, 0)
            t = api_towers[idx % len(api_towers)]
            _metro_tower_idx[key] = idx + 1
            tx_lat = t["tx_lat"]
            tx_lon = t["tx_lon"]
            tx_alt_ft = t["tx_alt_ft"]
            fc_hz = t["fc_hz"]
            callsign = t["tx_callsign"]

        # Place RX on land (rejects water positions)
        rx_lat, rx_lon = _place_rx_on_land(tx_lat, tx_lon, dist_min_km=5, dist_max_km=40)
        rx_alt_ft = random.uniform(100, 2000)

        node_fc = fc_hz + random.choice([-500000, 0, 0, 0, 500000])
        beam_width = random.uniform(35, 45)
        max_range = random.uniform(35, 55)

        region_prefix = region.upper()
        node_id = f"synth-{region_prefix}-{i + 1:04d}"

        node = GeneratedNodeConfig(
            node_id=node_id,
            rx_lat=rx_lat,
            rx_lon=rx_lon,
            rx_alt_ft=round(rx_alt_ft, 1),
            tx_lat=tx_lat,
            tx_lon=tx_lon,
            tx_alt_ft=tx_alt_ft,
            fc_hz=node_fc,
            fs_hz=2_000_000,
            beam_width_deg=round(beam_width, 1),
            max_range_km=round(max_range, 1),
            region=region,
            tx_callsign=callsign,
        )
        nodes.append(asdict(node))

    # --- Solo nodes (isolated — strictly one node per unique tower position) ---
    if n_solo > 0:
        # All US positions that must be avoided when extending the pool
        # Avoid positions: metro towers only.  Named solo towers are gated
        # inside _extend_solo_pool with the same min_sep check so they are
        # also enforced to be at least min_sep_km from each other.
        # 400 km > 2 × 140 km fleet range → solo coverage circles never
        # touch each other or any metro cluster, making them visually isolated.
        us_metro_occ: list[tuple[float, float]] = [
            (t[0], t[1]) for t in _TOWERS_US
        ]
        solo_pool = _extend_solo_pool(
            list(solo_towers),
            n_solo,
            avoid_positions=us_metro_occ,
            min_sep_km=400.0,
        )
        random.shuffle(solo_pool)

        for j in range(min(n_solo, len(solo_pool))):
            tower = solo_pool[j]          # strict: never re-use a tower index
            tx_lat, tx_lon, tx_alt_ft, fc_hz, callsign = tower

            # Place RX on land (rejects water positions)
            rx_lat, rx_lon = _place_rx_on_land(tx_lat, tx_lon, dist_min_km=8, dist_max_km=35)
            rx_alt_ft = random.uniform(100, 1500)

            beam_width = random.uniform(35, 45)
            max_range = random.uniform(35, 55)

            node_id = f"synth-SOLO-{j + 1:04d}"

            node = GeneratedNodeConfig(
                node_id=node_id,
                rx_lat=round(rx_lat, 6),
                rx_lon=round(rx_lon, 6),
                rx_alt_ft=round(rx_alt_ft, 1),
                tx_lat=tx_lat,
                tx_lon=tx_lon,
                tx_alt_ft=tx_alt_ft,
                fc_hz=fc_hz,
                fs_hz=2_000_000,
                beam_width_deg=round(beam_width, 1),
                max_range_km=round(max_range, 1),
                region="us",
                tx_callsign=callsign,
            )
            nodes.append(asdict(node))

    return nodes


def fleet_summary(nodes: list[dict]) -> dict:
    """Compute a summary of the fleet configuration."""
    from collections import Counter
    regions = Counter(n["region"] for n in nodes)
    towers = Counter(n["tx_callsign"] for n in nodes)
    return {
        "total_nodes": len(nodes),
        "regions": dict(regions),
        "unique_towers": len(towers),
        "towers_by_usage": dict(towers.most_common(20)),
        "lat_range": (
            round(min(n["rx_lat"] for n in nodes), 4),
            round(max(n["rx_lat"] for n in nodes), 4),
        ),
        "lon_range": (
            round(min(n["rx_lon"] for n in nodes), 4),
            round(max(n["rx_lon"] for n in nodes), 4),
        ),
    }


def main():
    parser = argparse.ArgumentParser(description="Generate fleet of synthetic node configs")
    parser.add_argument("--nodes", type=int, default=200, help="Number of nodes (100-1000)")
    parser.add_argument("--regions", type=str, default="us", help="Comma-separated regions: us,eu,au")
    parser.add_argument("--output", type=str, default="fleet_config.json", help="Output file path")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    args = parser.parse_args()

    regions = [r.strip().lower() for r in args.regions.split(",")]
    nodes = generate_fleet(n_nodes=args.nodes, regions=regions, seed=args.seed)
    summary = fleet_summary(nodes)

    config = {
        "fleet": {
            "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "summary": summary,
        },
        "nodes": nodes,
    }

    with open(args.output, "w") as f:
        json.dump(config, f, indent=2)

    print(f"Generated {len(nodes)} nodes → {args.output}", file=sys.stderr)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
