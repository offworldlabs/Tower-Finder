#!/usr/bin/env python3
"""Fail if staging and production have diverged anywhere they shouldn't.

Staging exists to catch problems before production sees them, which only works
while the two are the same everywhere that matters. They were not: staging had
no volume for the detection archive (so the dashboard's Data Explorer was
permanently empty), no Content-Security-Policy on any vhost, no /api/auth/ rate
limit, and no API key on the fleet's ingest — none of it deliberate, all of it
accumulated by editing two hand-maintained copies of the same config.

Two checks:

1. **Compose.** Merge base + each overlay via `docker compose config`, then walk
   both trees. Every leaf that differs must be covered by ALLOWED_DIVERGENCE.
   Anything else fails, naming the exact key path.

2. **nginx.** Render the shared template with each environment's HOST_* values,
   then rewrite every hostname back to a per-role token. The two results must be
   byte-identical: same vhosts, same headers, same rate limits, same locations.

Run locally with:  python3 deploy/check-env-parity.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = "docker-compose.yml"
OVERLAYS = {"production": "docker-compose.prod.yml", "staging": "docker-compose.staging.yml"}

# Every vhost the template defines must be TLS in a deployed environment. Update
# this alongside the template if a vhost is added or removed.
EXPECTED_TLS_VHOSTS = 7

# Key paths permitted to differ between the environments, as regexes matched
# against the dotted path into the merged compose tree.
#
# Adding an entry here is how you record a deliberate difference. Do that in the
# same commit as the change itself — an unexplained entry is how the previous
# divergence started.
ALLOWED_DIVERGENCE = (
    # Different droplets, different container names.
    r"^services\.[^.]+\.container_name$",
    # Different droplet sizes.
    r"^services\.[^.]+\.deploy\.resources\.limits\.(cpus|memory)$",
    # Which environment this is, and the hostnames that follow from it.
    r"^services\.tower-finder\.environment\.RETINA_ENV$",
    r"^services\.tower-finder\.environment\.CORS_ORIGINS$",
    r"^services\.tower-finder\.environment\.CSP_CONNECT_SRC$",
    r"^services\.tower-finder\.environment\.HOST_[A-Z_]+$",
    # Simulation scale — the one thing staging is *meant* to differ on.
    r"^services\.fleet\.environment\.FLEET_[A-Z_]+$",
    # Production alone joins the external edge network that fronts
    # tower-finder-service; staging has no such stack.
    r"^services\.tower-finder\.networks(\..*)?$",
    r"^networks(\..*)?$",
    # Compose records the file list it was assembled from.
    r"^name$",
    r"^services\.[^.]+\.(build|image)\.?.*labels.*$",
)

_ALLOWED = tuple(re.compile(p) for p in ALLOWED_DIVERGENCE)

# Hostname roles, mapped back to a common token before comparing the two
# rendered nginx configs. Ordered longest-value-first at substitution time so
# `staging.retina.fm` cannot shadow `staging-map.retina.fm`.
HOST_VARS = (
    "HOST_MAIN",
    "HOST_API",
    "HOST_MAP",
    "HOST_DASH",
    "HOST_ADMIN",
    "HOST_TESTMAP",
    "HOST_LEGACY_REDIRECT",
    "CSP_CONNECT_SRC",
)


def compose_config(overlay: str) -> dict:
    out = subprocess.run(
        ["docker", "compose", "-f", BASE, "-f", overlay, "config", "--format", "json"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        hint = ""
        if not (REPO / "backend" / ".env").exists():
            hint = (
                "\nhint: backend/.env is missing. Both services declare it as an env_file "
                "and compose will not resolve without one. `touch backend/.env` — this "
                "check compares the overlays with each other and reads nothing from it."
            )
        raise SystemExit(
            f"`docker compose -f {BASE} -f {overlay} config` failed:\n{out.stderr}{hint}"
        )
    return json.loads(out.stdout)


def flatten(node, prefix: str = "") -> dict[str, object]:
    """Flatten nested dicts/lists to {dotted.path: scalar}."""
    flat: dict[str, object] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            flat.update(flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        # Sort scalar lists so an ordering difference isn't reported as drift.
        try:
            items = sorted(node, key=repr)
        except TypeError:
            items = node
        for i, value in enumerate(items):
            flat.update(flatten(value, f"{prefix}[{i}]"))
    else:
        flat[prefix] = node
    return flat


def allowed(path: str) -> bool:
    bare = re.sub(r"\[\d+\]", "", path)
    return any(p.search(path) or p.search(bare) for p in _ALLOWED)


def check_compose() -> list[str]:
    trees = {env: flatten(compose_config(overlay)) for env, overlay in OVERLAYS.items()}
    prod, staging = trees["production"], trees["staging"]

    problems = []
    for path in sorted(set(prod) | set(staging)):
        in_prod, in_staging = prod.get(path, "<absent>"), staging.get(path, "<absent>")
        if in_prod == in_staging or allowed(path):
            continue
        problems.append(f"  {path}\n      production: {in_prod!r}\n      staging:    {in_staging!r}")
    return problems


def render(env_values: dict[str, str], out_path: Path) -> str:
    env_file = out_path.with_suffix(".env")
    env_file.write_text("".join(f"{k}={v}\n" for k, v in env_values.items()))
    result = subprocess.run(
        [
            sys.executable,
            str(REPO / "deploy" / "render-nginx-config.py"),
            str(REPO / "deploy" / "nginx" / "nginx.conf.template"),
            str(out_path),
            "--env-file",
            str(env_file),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"rendering nginx config failed:\n{result.stderr}")
    return out_path.read_text()


def check_nginx(tmp: Path) -> list[str]:
    rendered = {}
    for env, overlay in OVERLAYS.items():
        service_env = compose_config(overlay)["services"]["tower-finder"]["environment"]
        values = {k: service_env[k] for k in HOST_VARS}
        # Pass TLS_ENABLED through when the overlay sets it, so the render below
        # reflects what the environment would actually serve. Without this the
        # renderer always takes its TLS-on default and the assertion further down
        # could never fail, however badly an overlay was misconfigured.
        if "TLS_ENABLED" in service_env:
            values["TLS_ENABLED"] = service_env["TLS_ENABLED"]
        text = render(values, tmp / f"{env}.conf")
        # Replace each environment's hostnames with a role token so only
        # structural differences survive. Longest first: a short hostname can be
        # a substring of a longer one (`map.retina.fm` inside
        # `staging-map.retina.fm`), and replacing the short one first would
        # corrupt the comparison.
        for var, value in sorted(values.items(), key=lambda kv: len(kv[1]), reverse=True):
            text = text.replace(value, f"<{var}>")
        rendered[env] = text

    # Absolute assertion, not a comparison. The parity diff below only proves the
    # two environments match EACH OTHER, so a change that dropped TLS from both
    # would sail through it. render-nginx-config.py can now render a plain-HTTP
    # variant for the laptop (TLS_ENABLED=false), which makes that reachable for
    # the first time — so pin the shape of the deployed environments directly.
    problems: list[str] = []
    for env, text in rendered.items():
        listeners = text.count("listen 443 ssl;")
        if listeners != EXPECTED_TLS_VHOSTS:
            problems.append(
                f"  {env}: {listeners} `listen 443 ssl` blocks, expected "
                f"{EXPECTED_TLS_VHOSTS}. A deployed environment must not render "
                f"plain HTTP; check TLS_ENABLED and the RETINA_IF TLS blocks."
            )
        if "ssl_certificate " not in text:
            problems.append(f"  {env}: rendered config has no ssl_certificate directive.")
        if "Strict-Transport-Security" not in text:
            problems.append(f"  {env}: rendered config has no HSTS header.")
    if problems:
        return problems

    if rendered["production"] == rendered["staging"]:
        return []

    import difflib

    diff = list(
        difflib.unified_diff(
            rendered["production"].splitlines(),
            rendered["staging"].splitlines(),
            "production (hostnames tokenised)",
            "staging (hostnames tokenised)",
            lineterm="",
            n=1,
        )
    )
    return ["  " + line for line in diff]


def main() -> int:
    failures = []

    compose_problems = check_compose()
    if compose_problems:
        failures.append(
            "Compose: staging and production differ at key paths that are not in\n"
            "ALLOWED_DIVERGENCE. Either move the setting into docker-compose.yml so both\n"
            "environments share it, or — if the difference is genuinely intended — add the\n"
            "key to ALLOWED_DIVERGENCE in this file, in the same commit.\n\n"
            + "\n".join(compose_problems)
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        nginx_problems = check_nginx(Path(tmpdir))
    if nginx_problems:
        failures.append(
            "nginx: the shared template renders differently for the two environments once\n"
            "hostnames are normalised. Every vhost, header, rate limit and location must\n"
            "match — that is what makes staging a valid rehearsal for production.\n\n"
            + "\n".join(nginx_problems)
        )

    if failures:
        print("\n\n".join(failures), file=sys.stderr)
        return 1

    print("staging and production are in parity (compose + nginx)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
