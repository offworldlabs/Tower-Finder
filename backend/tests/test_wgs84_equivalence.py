"""Cross-lib guard: the two WGS84 implementations must agree.

retina-tracker/geometry.py and retina-geolocator/Geometry.py each carry their
own ecef2lla (different iteration form, different pole handling).  Measured
over the CONUS operating envelope they agree to 5.6e-11 degrees, so neither
is replaced — but nothing guarded that agreement until this test.  Lives in
backend/tests because it is the one place both libraries are installed
together; the libs' own CI runs them in isolation.
"""

import os
import random

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "test-key-abc123")

from retina_geolocator.Geometry import Geometry as GeoG  # noqa: E402
from retina_tracker.geometry import ecef2lla as trk_ecef2lla  # noqa: E402
from retina_tracker.geometry import lla2ecef as trk_lla2ecef  # noqa: E402


class TestEcefLlaAgreement:
    def test_round_trip_agrees_across_libraries(self):
        rng = random.Random(7)
        worst_deg = 0.0
        worst_alt = 0.0
        for _ in range(500):
            lat = rng.uniform(-70, 70)
            lon = rng.uniform(-180, 180)
            alt = rng.uniform(0, 20_000)
            x, y, z = GeoG.lla2ecef(lat, lon, alt)
            g = GeoG.ecef2lla(x, y, z)
            t = trk_ecef2lla(x, y, z)
            worst_deg = max(worst_deg, abs(g[0] - t[0]), abs(g[1] - t[1]))
            worst_alt = max(worst_alt, abs(g[2] - t[2]))
        # ~1e-9 deg is ~0.1 mm.  If either implementation drifts, this trips
        # long before the solvers notice.
        assert worst_deg < 1e-9, worst_deg
        assert worst_alt < 1e-3, worst_alt

    def test_lla2ecef_agrees_across_libraries(self):
        rng = random.Random(9)
        for _ in range(200):
            lat = rng.uniform(-89, 89)
            lon = rng.uniform(-180, 180)
            alt = rng.uniform(0, 20_000)
            gx, gy, gz = GeoG.lla2ecef(lat, lon, alt)
            tx, ty, tz = trk_lla2ecef(lat, lon, alt)
            assert max(abs(gx - tx), abs(gy - ty), abs(gz - tz)) < 1e-6


class TestSpeedOfLightSingleSource:
    def test_geolocator_constants_are_consistent(self):
        from retina_geolocator.constants import C_KM_S, C_KM_US, C_M_S
        assert C_M_S == 299_792_458.0
        assert C_KM_S == C_M_S / 1000.0
        assert C_KM_US == C_M_S / 1e9

    def test_analytics_agrees(self):
        from retina_analytics.constants import C_KM_US as A_C_KM_US
        from retina_geolocator.constants import C_KM_US as G_C_KM_US
        assert A_C_KM_US == G_C_KM_US
