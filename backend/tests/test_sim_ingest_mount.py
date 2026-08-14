"""The gate on the simulation ingest mount.

routes/sim_ingest.py holds the two POSTs the synthetic fleet writes
state.adsb_aircraft and state.ground_truth_trails through, and main.py mounts
that router only when `synthetic_fleet_enabled` says so. Since ClickUp 86cb49d29
that rule is the whole of the gate on the write path: the mount used to follow
from RETINA_ENV not being `production`, so a deployment got the path for being
named `test`. Dropping the check would silently reopen the path everywhere
rather than fail anything, which is what these pin.

The rule is exercised directly and the wiring separately, because only the
wiring needs a child interpreter. Two probes rather than nine keeps this under
six seconds; the value parsing costs nothing as a function call.
"""

import os

import pytest

from routes.sim_ingest import synthetic_fleet_enabled
from tests.probe_helpers import run_probe

# The routes the gate governs. They share one router, so mounting is
# all-or-nothing; naming them individually is what makes a rename or a move to
# another router show up here rather than pass quietly.
SIM_INGEST_ROUTES = frozenset({"/api/sim/adsb/push", "/api/test/ground-truth/push"})

# A route from a router mounted unconditionally. Asserting it is present in
# every probe is the control: without it, an absent ingest path could equally
# mean the child interpreter failed before building the app.
ALWAYS_MOUNTED_ROUTE = "/api/admin/events"


class TestSyntheticFleetGate:
    """The rule itself, which no environment name may satisfy."""

    @pytest.mark.parametrize("env_name", ["dev", "test", "staging", "production"])
    def test_no_environment_name_opens_the_gate(self, env_name):
        """The regression guard. `test` is the case that changed: it used to
        mount the router on the name alone."""
        assert synthetic_fleet_enabled({"RETINA_ENV": env_name}) is False

    @pytest.mark.parametrize("env_name", ["dev", "test", "staging", "production"])
    def test_the_flag_opens_it_in_any_environment(self, env_name):
        assert synthetic_fleet_enabled({"RETINA_ENV": env_name, "SYNTHETIC_FLEET_ENABLED": "1"}) is True

    @pytest.mark.parametrize("value", ["", "0", "true", "True", "yes", "on", " 1", "1 "])
    def test_only_the_literal_one_opens_it(self, value):
        """Matches AUTH_ALLOW_ANONYMOUS_ADMIN: exactly "1", nothing else.

        Compose passes values through verbatim, so `SYNTHETIC_FLEET_ENABLED=true`
        in an overlay has to leave a write path shut rather than half-open.
        """
        assert synthetic_fleet_enabled({"SYNTHETIC_FLEET_ENABLED": value}) is False


_MOUNT_PROBE = """
import json

import main

print("PROBE:" + json.dumps({"paths": sorted({route.path for route in main.app.routes})}))
"""


def _probe_mounted_paths(*, flag: str | None, db_path) -> set[str]:
    """Import main in a subprocess and report the paths the app ended up with.

    A subprocess because the mount decision is made once, while main.py is
    imported, and pytest's own interpreter has already imported it under
    conftest.py's environment.

    RETINA_ENV=production in both runs is the point: it is the environment that
    used to be the only one denied the mount, so it demonstrates the name no
    longer decides either way. JWT_SECRET comes with it, since core/users.py
    refuses to import without one under that name.
    """
    env = os.environ | {
        "RETINA_ENV": "production",
        "RETINA_DB_PATH": str(db_path),
        "JWT_SECRET": "0" * 32,
    }
    # Absent has to mean absent rather than empty: conftest.py does not set this
    # flag today, but a developer's shell might.
    env.pop("SYNTHETIC_FLEET_ENABLED", None)
    if flag is not None:
        env["SYNTHETIC_FLEET_ENABLED"] = flag

    paths = set(run_probe(_MOUNT_PROBE, env)["paths"])
    assert ALWAYS_MOUNTED_ROUTE in paths, "app did not build; the probe proves nothing"
    return paths


def test_main_consults_the_gate(tmp_path):
    """The wiring: main.py must actually mount on the rule above, not near it.

    Both halves in one test because each costs an interpreter boot, and a
    one-sided assertion would pass against a mount that is stuck open or stuck
    shut.
    """
    with_flag = _probe_mounted_paths(flag="1", db_path=tmp_path / "with_flag.db")
    assert with_flag >= SIM_INGEST_ROUTES

    without_flag = _probe_mounted_paths(flag=None, db_path=tmp_path / "no_flag.db")
    assert SIM_INGEST_ROUTES.isdisjoint(without_flag)
