"""Shared child-interpreter probe helper.

Parts of the backend decide their behaviour once, as they import, from the
process environment: core/users.py derives AUTH_BYPASS at module load, and
main.py chooses which routers to mount while building the app. Neither can be
re-examined under a different environment inside the interpreter pytest is
already running in — conftest.py fixes that environment before the first
import, and by the time a test runs the derived values have been copied into
other modules, so patching or reloading leaves stale duplicates behind. A child
interpreter with an environment of its own is the only way to observe the other
branch.

A caller supplies a script that prints exactly one PROBE line of JSON;
`run_probe` executes it from the backend root under `env` and returns the
parsed object. Anything else the script writes is kept for the failure message.
"""

import json
import subprocess
import sys
from collections.abc import Mapping

from tests.migration_helpers import BACKEND

# Prefixed rather than printed bare so the assertion can distinguish the
# probe's own line from whatever the backend logs on its way up.
PROBE_PREFIX = "PROBE:"


def run_probe(script: str, env: Mapping[str, str]) -> dict:
    """Run `script` in a child interpreter and return its decoded PROBE line."""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND,
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = next((ln for ln in result.stdout.splitlines() if ln.startswith(PROBE_PREFIX)), None)
    assert line is not None, result.stdout + result.stderr
    return json.loads(line.removeprefix(PROBE_PREFIX))
