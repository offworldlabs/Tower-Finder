#!/usr/bin/env bash
# Copy a compose project's named volumes to a new project prefix.
#
# Docker cannot rename a volume, so a project rename means copy and swap. The
# source volumes are only ever read, so this is safe to re-run and the old data
# stays available as the rollback until it is deliberately pruned.
set -euo pipefail

# find plus stat rather than du: busybox du has no -b, and du reports
# allocated blocks, which differ between volumes for identical content. This
# is the cheap first gate; it catches a truncated or short copy but is blind
# to same-size corruption, and to ownership and permissions entirely, both of
# which manifest() below catches instead.
#
# set -e is load-bearing here, not decorative: pipefail alone only fixes the
# exit status of a pipeline that is itself the last thing the script runs,
# and here the count and the byte sum are command substitutions embedded as
# printf arguments, so their exit status would otherwise never reach printf's
# own. Assigning each to a variable first gives set -e something to catch: a
# failing find/stat under a failing $(...) assignment aborts the script
# immediately, which is what makes docker run's own exit code trustworthy.
measure() {
    docker run --rm -v "$1":/v:ro alpine sh -c '
        set -eo pipefail
        n=$(find /v -type f | wc -l)
        b=$(find /v -type f -exec stat -c %s {} + | awk "{s+=\$1} END {print s+0}")
        printf "%s %s" "$n" "$b"
    '
}

# Hashes every regular file and records every directory and symlink by path,
# a symlink by its target rather than by following it, and every entry's
# owner, group and mode, so a copy that silently changes ownership (a stray
# chown or chmod on the destination) shows up here even though it changes
# no byte and no path. `stat` on a symlink reports the link's own uid/gid/
# mode rather than the target's (verified: busybox stat does not dereference
# by default), which is what makes the symlink line describe the link and
# not whatever it points at. find -type f alone never counts a symlink or an
# empty directory, so without the second and third find calls a dropped
# symlink or a dropped empty directory changes nothing on either side of the
# count/byte check above. The three listings are sorted, to remove find's
# traversal-order noise, and folded into one digest so only a single line has
# to cross back out of the container; sort and sha256sum only care about full
# line content, so the three listings not sharing a column layout is fine.
#
# set -e is load-bearing for the same reason as in measure() above: without
# it, a failing find or readlink partway through one of the three listings
# below still leaves the group's exit status set by whichever of the three
# commands happens to run last, so a partial listing would be hashed and the
# result would still be a valid-looking digest. With set -e, a failing
# command aborts the group immediately, and pipefail then carries that
# failure through sort/sha256sum/cut to the exit code docker run reports.
manifest() {
    docker run --rm -v "$1":/v:ro alpine sh -c '
        set -eo pipefail
        cd /v
        {
            find . -type f | while IFS= read -r p; do
                meta="$(stat -c "%u %g %a" "$p")"
                line="$(sha256sum "$p")"
                printf "F %s %s\n" "$meta" "$line"
            done
            find . -type l | while IFS= read -r p; do
                meta="$(stat -c "%u %g %a" "$p")"
                target="$(readlink "$p")"
                printf "L %s %s  L:%s\n" "$meta" "$p" "$target"
            done
            # Includes "." itself, the mount root, on purpose. busybox cp -a
            # applies -p semantics to the target directory node as well as its
            # contents, so a source root chowned to appuser (which is what the
            # Dockerfile does to /app/backend/data and friends) is reproduced on
            # the destination and the two agree. GNU cp does not do this, so if
            # the base image ever leaves busybox the manifests will stop matching.
            # That is a loud verification failure rather than a silent bad copy,
            # which is the direction this script should fail in. The happy path in
            # backend/tests/test_migrate_volumes.sh chowns the volume root for
            # exactly this reason, so the behaviour stays pinned.
            find . -type d | while IFS= read -r p; do
                meta="$(stat -c "%u %g %a" "$p")"
                printf "D %s %s\n" "$meta" "$p"
            done
        } | sort | sha256sum | cut -d" " -f1
    '
}

# A measurement or manifest that comes back empty must not read as two sides
# agreeing on nothing. Capturing each docker run on its own line, below, lets
# set -e catch the docker run itself failing (a `read ... <<<"$(cmd)"` would
# have set -e inspect read's own exit status rather than the substitution's,
# so a failing cmd would pass silently). check_digits and check_digest are
# the second line of defence, for a docker run that exits zero but still
# produced nothing usable inside the container.
check_digits() {
    local label="$1" field="$2" value="$3" raw="$4"
    case "$value" in
        ''|*[!0-9]*)
            echo "✗ could not measure $label: bad $field (got '$raw')" >&2
            exit 1
            ;;
    esac
}

check_digest() {
    local label="$1" value="$2"
    case "$value" in
        ''|*[!0-9a-f]*)
            echo "✗ could not build manifest for $label: bad digest (got '$value')" >&2
            exit 1
            ;;
    esac
}

# Kept separate from the copy step in main() below so it can be exercised
# against a deliberately corrupted destination on its own: a full re-run of
# main() would just copy over any corruption again before checking anything.
verify_volume() {
    local src="$1" dst="$2" vol="$3"
    local src_raw dst_raw src_files src_bytes dst_files dst_bytes
    local src_manifest dst_manifest

    src_raw="$(measure "$src")" || { echo "✗ could not measure $src: docker run failed" >&2; exit 1; }
    dst_raw="$(measure "$dst")" || { echo "✗ could not measure $dst: docker run failed" >&2; exit 1; }
    read -r src_files src_bytes <<<"$src_raw"
    read -r dst_files dst_bytes <<<"$dst_raw"
    check_digits "$src" "file count" "$src_files" "$src_raw"
    check_digits "$src" "byte count" "$src_bytes" "$src_raw"
    check_digits "$dst" "file count" "$dst_files" "$dst_raw"
    check_digits "$dst" "byte count" "$dst_bytes" "$dst_raw"

    if [ "$src_files" != "$dst_files" ] || [ "$src_bytes" != "$dst_bytes" ]; then
        echo "✗ $vol mismatch: ${src_files}f/${src_bytes}b in $src to ${dst_files}f/${dst_bytes}b in $dst" >&2
        exit 1
    fi

    src_manifest="$(manifest "$src")" || { echo "✗ could not build manifest for $src: docker run failed" >&2; exit 1; }
    dst_manifest="$(manifest "$dst")" || { echo "✗ could not build manifest for $dst: docker run failed" >&2; exit 1; }
    check_digest "$src" "$src_manifest"
    check_digest "$dst" "$dst_manifest"

    if [ "$src_manifest" != "$dst_manifest" ]; then
        echo "✗ $vol content mismatch in $dst: manifest $src_manifest in $src, $dst_manifest in $dst" >&2
        exit 1
    fi

    echo "✓ $vol verified: ${dst_files} files, ${dst_bytes} bytes, manifest ${dst_manifest}"
}

main() {
    if [ "$#" -lt 3 ]; then
        echo "usage: $0 <old-project> <new-project> <volume>..." >&2
        exit 2
    fi

    local old_project new_project vol src dst
    old_project="$1"; shift
    new_project="$1"; shift

    for vol in "$@"; do
        src="${old_project}_${vol}"
        dst="${new_project}_${vol}"

        if ! docker volume inspect "$src" >/dev/null 2>&1; then
            echo "✗ source volume $src does not exist" >&2
            exit 1
        fi

        echo "→ $src to $dst"
        docker volume create "$dst" >/dev/null

        # cp -a preserves ownership, permissions and timestamps. The app runs
        # as a non-root uid and a flattened copy would leave it unable to
        # write.
        docker run --rm -v "$src":/from:ro -v "$dst":/to alpine \
            sh -c 'cd /from && cp -a . /to/'

        verify_volume "$src" "$dst" "$vol"
    done
}

# Guarded so the functions above can be sourced for testing without also
# running a migration: a plain invocation has BASH_SOURCE[0] equal to $0,
# sourcing does not.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    main "$@"
fi
