#!/usr/bin/env python3
"""Render deploy/nginx/nginx.conf.template into a concrete nginx config.

One template serves every deployed environment; production and staging differ
only in the hostnames and the CSP's connect-src. This replaced a hand-maintained
nginx.conf + nginx-staging.conf pair that had silently diverged — staging had no
Content-Security-Policy on any vhost and no /api/auth/ rate limit, so both were
untested until a change reached production.

Two passes over the template:

1. ``# RETINA_INCLUDE <path>`` lines are replaced by the contents of
   ``deploy/nginx/<path>``, preserving the marker's indentation. Snippets may
   include other snippets. This is a build-time inline rather than an nginx
   ``include`` directive on purpose: the rendered result is a single
   self-contained file written to the one path start.sh can write, and CI can
   diff two renders without a container or any nginx runtime state.

2. ``${VAR}`` is substituted for VAR in SUBSTITUTIONS — and *only* those. That
   allowlist is the whole point: a blanket substitution would also expand
   nginx's own runtime variables ($host, $remote_addr, $uri) and yield a config
   that parses cleanly while routing nothing correctly. Anything left looking
   like ``${...}`` after substitution is a typo, and is an error.

Usage:
    render-nginx-config.py <template> <output>          # values from os.environ
    render-nginx-config.py <template> <output> --env-file <file>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# The complete set of variables the template may reference. Keep in sync with
# the HOST_* / CSP_CONNECT_SRC entries in docker-compose.{prod,staging}.yml.
SUBSTITUTIONS = (
    "HOST_MAIN",
    "HOST_API",
    "HOST_MAP",
    "HOST_DASH",
    "HOST_ADMIN",
    "HOST_TESTAPI",
    "HOST_TESTMAP",
    "HOST_LEGACY_REDIRECT",
    "CSP_CONNECT_SRC",
)

_INCLUDE_RE = re.compile(r"^(\s*)#\s*RETINA_INCLUDE\s+(\S+)\s*$")
_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_MAX_INCLUDE_DEPTH = 10


def expand_includes(path: Path, root: Path, _depth: int = 0, _stack: tuple = ()) -> str:
    """Inline every ``# RETINA_INCLUDE`` marker, recursively."""
    if _depth > _MAX_INCLUDE_DEPTH:
        raise RuntimeError(
            f"include nesting deeper than {_MAX_INCLUDE_DEPTH} at {path} "
            f"(cycle? include chain: {' -> '.join(str(p) for p in _stack)})"
        )
    if path in _stack:
        chain = " -> ".join(str(p) for p in (*_stack, path))
        raise RuntimeError(f"circular include: {chain}")

    out: list[str] = []
    for lineno, line in enumerate(path.read_text().splitlines(), 1):
        m = _INCLUDE_RE.match(line)
        if not m:
            out.append(line)
            continue
        indent, rel = m.group(1), m.group(2)
        target = (root / rel).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"{path}:{lineno}: no such snippet: {rel}")
        if root.resolve() not in target.parents:
            raise RuntimeError(f"{path}:{lineno}: snippet escapes {root}: {rel}")
        nested = expand_includes(target, root, _depth + 1, (*_stack, path))
        out.extend(indent + s if s else "" for s in nested.splitlines())
    return "\n".join(out) + "\n"


def substitute(text: str, values: dict[str, str]) -> str:
    """Replace ${VAR} for the allowlisted VARs only, then verify none remain."""
    missing = [name for name in SUBSTITUTIONS if not values.get(name)]
    if missing:
        raise RuntimeError(
            "unset or empty in the environment: "
            + ", ".join(missing)
            + " — the compose overlay is probably missing (see deploy/env.*.example)"
        )

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name in values:
            return values[name]
        # Leave it alone here so the check below can report it with context.
        return m.group(0)

    rendered = _PLACEHOLDER_RE.sub(repl, text)

    leftovers = sorted({m.group(1) for m in _PLACEHOLDER_RE.finditer(rendered)})
    if leftovers:
        raise RuntimeError(
            "template references unknown variable(s): "
            + ", ".join(leftovers)
            + f" — add them to SUBSTITUTIONS in {Path(__file__).name} and to both compose overlays"
        )
    return rendered


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a KEY=VALUE file. Used by the CI parity check to render both
    environments without needing either host's real environment."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        help="read substitution values from this KEY=VALUE file instead of the environment",
    )
    args = parser.parse_args(argv)

    values = read_env_file(args.env_file) if args.env_file else dict(os.environ)

    try:
        expanded = expand_includes(args.template, args.template.parent)
        rendered = substitute(expanded, values)
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"render-nginx-config: {exc}", file=sys.stderr)
        return 1

    args.output.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
