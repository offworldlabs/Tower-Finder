"""Smoke test: drive a frame through archive_detections and read it back."""

import shutil
from pathlib import Path

import services.storage as st
from services.storage import archive_detections, read_archived_file


def main():
    base = Path("/tmp/parquet_smoke")
    if base.exists():
        shutil.rmtree(base)
    base.mkdir()

    st._LOCAL_ARCHIVE_DIR = str(base)

    frame = {
        "timestamp": 1700000000000,
        "delay": [12.34, 56.78],
        "doppler": [-100.5, 33.3],
        "snr": [15.0, 22.0],
        "adsb": [
            {"hex": "abcdef", "lat": 40.71, "lon": -74.0,
             "alt_baro": 35000, "gs": 480, "track": 270, "flight": "UAL1"},
            None,
        ],
        "_signing_mode": "unknown",
        "_signature_valid": False,
    }
    key = archive_detections("smoke-node", [frame])
    print("wrote:", key)
    decoded = read_archived_file(key)
    print("decoded count:", decoded["count"])
    print("first detection delay:", decoded["detections"][0]["delay"])
    print("adsb match:", decoded["detections"][0]["adsb"][0])


if __name__ == "__main__":
    main()
