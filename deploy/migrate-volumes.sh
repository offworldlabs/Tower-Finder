#!/usr/bin/env bash
# Copy a compose project's named volumes to a new project prefix.
#
# Docker cannot rename a volume, so a project rename means copy and swap. The
# source volumes are only ever read, so this is safe to re-run and the old data
# stays available as the rollback until it is deliberately pruned.
set -euo pipefail

if [ "$#" -lt 3 ]; then
    echo "usage: $0 <old-project> <new-project> <volume>..." >&2
    exit 2
fi

OLD_PROJECT="$1"; shift
NEW_PROJECT="$1"; shift

# find plus stat rather than du: busybox du has no -b, and du reports
# allocated blocks, which differ between volumes for identical content.
measure() {
    docker run --rm -v "$1":/v:ro alpine sh -c \
        'printf "%s %s" "$(find /v -type f | wc -l)" \
         "$(find /v -type f -exec stat -c %s {} + | awk "{s+=\$1} END {print s+0}")"'
}

for vol in "$@"; do
    src="${OLD_PROJECT}_${vol}"
    dst="${NEW_PROJECT}_${vol}"

    if ! docker volume inspect "$src" >/dev/null 2>&1; then
        echo "✗ source volume $src does not exist" >&2
        exit 1
    fi

    echo "→ $src to $dst"
    docker volume create "$dst" >/dev/null

    # cp -a preserves ownership, permissions and timestamps. The app runs as a
    # non-root uid and a flattened copy would leave it unable to write.
    docker run --rm -v "$src":/from:ro -v "$dst":/to alpine \
        sh -c 'cd /from && cp -a . /to/'

    read -r src_files src_bytes <<<"$(measure "$src")"
    read -r dst_files dst_bytes <<<"$(measure "$dst")"

    if [ "$src_files" != "$dst_files" ] || [ "$src_bytes" != "$dst_bytes" ]; then
        echo "✗ $vol mismatch: ${src_files}f/${src_bytes}b to ${dst_files}f/${dst_bytes}b" >&2
        exit 1
    fi
    echo "✓ $vol verified: ${dst_files} files, ${dst_bytes} bytes"
done
