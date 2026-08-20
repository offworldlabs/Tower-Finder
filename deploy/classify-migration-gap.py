#!/usr/bin/env python3
"""Report how far the database is ahead of the tree a rollback is restoring.

A rollback restores the previous image and moves the source tree back. It does
not move the database: Alembic revisions are only ever applied forward, by
deploy/start.sh at boot, and downgrading unattended on a flaky-E2E trigger
would be destructive DDL nobody asked for. So the schema is left ahead of the
code running against it, which is safe for a revision that only added something
and wrong for one that dropped, renamed or narrowed.

start.sh cannot tell those apart. It runs inside the restored image, and the
revisions the database is ahead by are precisely the ones missing from that
image's migrations/versions/, so all it holds is a revision id it cannot
resolve. This runs on the host before the tree moves, where both trees are
reachable through git, and grades the gap from what each revision declares.

Reads the repository from the working directory rather than from __file__ so
the caller decides which tree is being examined; deploy/rollback.sh has already
cd'd to APP_DIR by the time it calls this.

Deliberately git and file reads only, no `docker compose`: every compose command
in rollback.sh resolves through ./.env's COMPOSE_FILE, which is gitignored and
so does not move with the tree. Reading the live revision out of the container
would make this the first casualty of the very mismatch resync_env_to_tree
exists to repair.

Usage:  python3 deploy/classify-migration-gap.py <target-ref>

Exit status:  0 gap empty or wholly additive, 1 needs a manual downgrade,
2 could not tell.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

VERSIONS_DIR = "backend/migrations/versions"
ADDITIVE = "additive"
DESTRUCTIVE = "destructive"
UNDECLARED = "undeclared"
SAFETY_RE = re.compile(r'^rollback_safety\s*=\s*["\'](\w+)["\']', re.MULTILINE)

RULE = "─" * 74
MANUAL_HEADER = f"── Database: MANUAL ACTION REQUIRED {RULE[36:]}"


class GitError(RuntimeError):
    """git refused, most often because the ref does not exist on this box."""


def _git(*args: str) -> str:
    result = subprocess.run(  # noqa: S603, S607
        ["git", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _versions_at(ref: str) -> list[str]:
    """Revision filenames present in `ref`, in chain order.

    Sorted by filename, which is chain order given the NNNN_ prefix every
    revision here carries.

    A ref predating Alembic has no versions directory at all. `git ls-tree`
    prints nothing for a path a tree does not contain rather than failing, so
    that case arrives as the empty list it should be.
    """
    listing = _git("ls-tree", "-r", "--name-only", ref, "--", VERSIONS_DIR)
    return sorted(
        name
        for name in (line.rsplit("/", 1)[-1] for line in listing.splitlines())
        if name.endswith(".py") and name != "__init__.py"
    )


def _safety_at_head(filename: str) -> str:
    """What a revision in the gap declares about itself.

    Read through `git show` rather than off disk, so both sides of the
    comparison come from git and neither depends on working-tree state.
    """
    match = SAFETY_RE.search(_git("show", f"HEAD:{VERSIONS_DIR}/{filename}"))
    if match is None or match.group(1) not in (ADDITIVE, DESTRUCTIVE):
        return UNDECLARED
    return match.group(1)


def _revision_of(filename: str) -> str:
    """`0004_beam_width_nullable.py` -> `0004`."""
    return filename.removesuffix(".py").split("_", 1)[0]


def classify(target_ref: str) -> tuple[str, bool]:
    """The report block, and whether the gap is safe to serve."""
    here = _versions_at("HEAD")
    restored = _versions_at(target_ref)
    gap = [name for name in here if name not in set(restored)]

    if not gap:
        # Careful with what this claims. HEAD stands in for where the database
        # is, which holds on the first rollback and not on a second: the first
        # left HEAD at the tag it restored, so a re-run compares that tag with
        # itself and finds nothing, while the database is still ahead from the
        # first. Say what was compared and let the reader draw that conclusion.
        return (
            f"── Database {RULE[12:]}\n"
            "  The rollback does NOT move the database. The tree being restored\n"
            f"  ({target_ref}) ships every revision this one does, so this rollback adds\n"
            "  no gap of its own. The comparison is between the two trees, not against\n"
            "  the database, so a gap an earlier rollback left does not show up here.\n"
        ), True

    graded = [(name, _safety_at_head(name)) for name in gap]
    safe = all(safety == ADDITIVE for _, safety in graded)
    width = max(len(name) for name, _ in graded)
    listing = "\n".join(f"    {name:<{width}}  {safety}" for name, safety in graded)
    plural = "" if len(gap) == 1 else "s"

    if safe:
        header = f"── Database {RULE[12:]}"
        verdict = "  All additive: the restored code does not read what they added, so it is\n  safe to serve."
    elif not restored:
        # No revision to downgrade to, and no safe one to infer. `base` is the
        # only target git can offer here and it is the wrong one: 0001's
        # downgrade drops the tables the pre-Alembic code creates for itself
        # with create_all, so it would destroy the accounts, invites, owners and
        # claim codes the restored code goes straight back to reading. Guessing
        # 0001 instead would be this tool inventing a boundary nobody declared.
        header = MANUAL_HEADER
        verdict = (
            "  The tree being restored predates the migration history entirely, so the\n"
            "  whole chain above is in the gap and there is no safe automatic target to\n"
            "  downgrade to. Going all the way to base would drop the baseline tables\n"
            "  the restored code still needs, having created them itself. Choose the\n"
            "  target by hand before trusting this rollback."
        )
    else:
        header = MANUAL_HEADER
        verdict = (
            "  The restored code will query a schema that no longer matches. Downgrade\n"
            "  before trusting this rollback:\n"
            "    docker compose exec server \\\n"
            f'        sh -c "cd /app/backend && python3 -m alembic downgrade {_revision_of(restored[-1])}"'
        )

    return (
        f"{header}\n"
        f"  The rollback does NOT move the database. It stays at {_revision_of(here[-1])}.\n"
        f"  It is {len(gap)} revision{plural} ahead of the tree being restored "
        f"({target_ref}):\n"
        f"{listing}\n"
        f"{verdict}\n"
    ), safe


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <target-ref>", file=sys.stderr)
        return 2

    try:
        report, safe = classify(sys.argv[1])
    except GitError as exc:
        # 2 rather than 1: "could not tell" and "told you it is unsafe" both
        # block a clean rollback, but only one of them has a downgrade command
        # to offer, and the caller prints this stderr in place of the report.
        # Under the same header as the destructive path, at the same width:
        # this is one of the two ungradable cases, and without it an operator
        # scanning an incident log for the Database block reads its absence as
        # the check never having run. rollback.sh folds this stderr into
        # DB_REPORT, so it lands in the block's place.
        print(f"{MANUAL_HEADER}\n  Could not classify the migration gap: {exc}", file=sys.stderr)
        return 2

    print(report, end="")
    return 0 if safe else 1


if __name__ == "__main__":
    sys.exit(main())
