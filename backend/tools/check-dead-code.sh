#!/usr/bin/env bash
#
# Dead-code gate. Shared across offworldlabs Python repos; canonical copy lives
# in offworldlabs/ops.
#
#   tools/check-dead-code.sh          # fail if anything unwhitelisted is dead
#   tools/check-dead-code.sh --list   # print findings without failing
#
# Why this wraps vulture rather than calling it directly:
#
# Tests are SCANNED but not REPORTED. Excluding tests entirely — the obvious
# setup — makes anything used only by tests look dead, which is the largest
# single source of false positives. Scanning them fixes that, but then unused
# test helpers become findings in their own right. So we scan everything and
# drop findings whose location is a test file.
#
# Build artefacts are excluded because they contain a stale copy of the source,
# which doubles every finding.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

EXCLUDE=".venv,scripts,htmlcov,__pycache__,node_modules,build,dist,*.egg-info"
# Framework-dispatched handlers: Flask (@bp/@app) and FastAPI (@router/@app).
DECORATORS="@app.*,@bp.*,@router.*"
WHITELIST=""
[ -f vulture_whitelist.py ] && WHITELIST="vulture_whitelist.py"

findings="$(vulture . $WHITELIST \
    --min-confidence 60 \
    --exclude "$EXCLUDE" \
    --ignore-decorators "$DECORATORS" \
    2>/dev/null | grep -vE '(^|/)tests?/|/test_|conftest\.py' || true)"

if [ -z "$findings" ]; then
    echo "no dead code found"
    exit 0
fi

echo "$findings"

if [ "${1:-}" = "--list" ]; then
    exit 0
fi

echo >&2
echo "Dead code found. Delete it, or — only if it is referenced dynamically and" >&2
echo "vulture cannot see that — add it to vulture_whitelist.py with a reason." >&2
exit 1
