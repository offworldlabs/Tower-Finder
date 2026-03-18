"""
Generate a multi-region synthetic node network configuration.

Produces nodes_config_test.json (or a custom path) with N nodes spread
across major US metro regions, each with a realistic FM broadcast tower
as the transmit reference and RX nodes scattered in the surrounding area.

Usage:
    python generate_test_network.py                  # 100 nodes → nodes_config_test.json
    python generate_test_network.py --nodes 1000     # 1000 nodes
    python generate_test_network.py --nodes 50 --out custom.json
    python generate_test_network.py --nodes 100 --list-regions
"""

import argparse
import json
import math
import random

# ── US metro regions ──────────────────────────────────────────────────────────
# (name, tx_lat, tx_lon, tx_alt_ft, center_lat, center_lon, fc_hz)
# TX sites are real FM broadcast tower clusters in each metro.
REGIONS = [
    ("atl",  33.75667, -84.33184, 1600, 33.85, -84.40,   195_000_000),
    ("nyc",  40.74818, -74.03082, 1000, 40.72, -74.05,    98_700_000),
    ("chi",  41.95850, -87.79501, 1500, 41.90, -87.80,   101_900_000),
    ("dal",  32.89959, -97.04022,  800, 32.90, -97.05,   103_700_000),
    ("lax",  34.11620, -118.36900, 2000, 34.05, -118.30,   97_100_000),
    ("sfo",  37.61690, -122.41750, 1200, 37.65, -122.40,  107_700_000),
    ("mia",  25.79550, -80.21292,  500, 25.81, -80.22,    99_900_000),
    ("sea",  47.54070, -122.28510, 1800, 47.50, -122.30,  104_900_000),
    ("bos",  42.36570, -71.02078,  950, 42.35, -71.03,    96_900_000),
    ("den",  39.86100, -104.67370, 2500, 39.85, -104.67,  105_100_000),
    ("phx",  33.42530, -112.00780, 1400, 33.43, -112.01,   98_300_000),
    ("stl",  38.74870, -90.37000,  1100, 38.75, -90.37,   102_300_000),
    ("hou",  29.98820, -95.34175,   600, 30.00, -95.35,   100_300_000),
    ("msp",  44.88480, -93.22230,  1200, 44.90, -93.20,   106_100_000),
    ("pdx",  45.58980, -122.59510, 1300, 45.60, -122.60,  103_300_000),
    ("clt",  35.21440, -80.94730,   900, 35.20, -80.95,    97_500_000),
    ("dtw",  42.21250, -83.35340,  1050, 42.25, -83.35,   102_700_000),
    ("slc",  40.78840, -111.97790, 2200, 40.80, -111.98,   99_300_000),
    ("mco",  28.43120, -81.30810,   450, 28.45, -81.30,   101_300_000),
    ("iah",  29.99340, -95.33640,   700, 30.00, -95.35,   104_300_000),
]


def _gen_rx(center_lat: float, center_lon: float,
            min_km: float = 5, max_km: float = 65,
            alt_ft_lo: int = 500, alt_ft_hi: int = 1400) -> tuple[float, float, int]:
    """Random RX position within [min_km, max_km] of centre."""
    angle = random.uniform(0, 2 * math.pi)
    dist = random.uniform(min_km, max_km)
    R = 6371.0
    dlat = (dist * math.cos(angle)) / R * (180 / math.pi)
    dlon = (dist * math.sin(angle)) / (R * math.cos(math.radians(center_lat))) * (180 / math.pi)
    return (
        round(center_lat + dlat, 6),
        round(center_lon + dlon, 6),
        random.randint(alt_ft_lo, alt_ft_hi),
    )


def generate(n_nodes: int, seed: int = 42) -> dict:
    """Return a nodes config dict with n_nodes entries spread across REGIONS."""
    random.seed(seed)
    nodes = []
    n_regions = len(REGIONS)
    base = n_nodes // n_regions
    extra = n_nodes - base * n_regions

    for i, (rname, tx_lat, tx_lon, tx_alt_ft, cx, cy, fc_hz) in enumerate(REGIONS):
        count = base + (1 if i < extra else 0)
        for j in range(count):
            rx_lat, rx_lon, rx_alt_ft = _gen_rx(cx, cy)
            nodes.append({
                "node_id": f"synth-{rname}-{j+1:03d}",
                "rx_lat": rx_lat,
                "rx_lon": rx_lon,
                "rx_alt_ft": rx_alt_ft,
                "tx_lat": tx_lat,
                "tx_lon": tx_lon,
                "tx_alt_ft": tx_alt_ft,
                "fc_hz": fc_hz,
                "fs_hz": 2_000_000,
                "beam_width_deg": 48,
                "max_range_km": 50,
            })

    return {"nodes": nodes}


def main():
    ap = argparse.ArgumentParser(description="Generate test node network config")
    ap.add_argument("--nodes", type=int, default=100, help="Number of nodes (default: 100)")
    ap.add_argument("--out", default="nodes_config_test.json", help="Output JSON file")
    ap.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    ap.add_argument("--list-regions", action="store_true", help="Print available regions and exit")
    args = ap.parse_args()

    if args.list_regions:
        print(f"{'Region':<8} {'TX lat':>10} {'TX lon':>11} {'FC MHz':>10}")
        print("-" * 46)
        for name, tx_lat, tx_lon, _, _, _, fc_hz in REGIONS:
            print(f"{name:<8} {tx_lat:>10.5f} {tx_lon:>11.5f} {fc_hz/1e6:>10.1f}")
        return

    config = generate(args.nodes, args.seed)
    out_path = args.out
    with open(out_path, "w") as f:
        json.dump(config, f, indent=2)

    n = len(config["nodes"])
    regions_used = len(REGIONS)
    print(f"Generated {n} nodes across {regions_used} regions → {out_path}")
    print(f"  ~{n // regions_used} nodes per region ({regions_used} regions)")


if __name__ == "__main__":
    main()
