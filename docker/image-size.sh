#!/bin/sh
# docker/image-size.sh <image> [budget_bytes]
#
# The image's compressed size, measured as `docker save | gzip -6 | wc -c`
# over the whole image, against a budget in bytes (default 180000000: the
# 180 MB of phase 0 finding 6, decimal). Prints one line,
#   compressed: 162.2 MB (budget 180.0 MB)
# and exits 0 within the budget, 1 over it, 2 on a usage error or an image
# that cannot be saved (never read as a small image).
set -eu

usage() {
    printf 'usage: image-size.sh <image> [budget_bytes]\n' >&2
    exit 2
}

[ "$#" -ge 1 ] && [ "$#" -le 2 ] || usage
image=$1
budget=${2-180000000}
case $budget in
    '' | *[!0-9]*) usage ;;
esac

if ! docker image inspect "$image" >/dev/null 2>&1; then
    printf 'image-size.sh: no such image: %s\n' "$image" >&2
    exit 2
fi

# A POSIX pipeline's status is its last command's: the save's goes through a file.
status=$(mktemp)
trap 'rm -f "$status"' EXIT
bytes=$( { docker save "$image" || echo failed >"$status"; } | gzip -6 | wc -c | tr -d " ")
if [ -s "$status" ]; then
    printf 'image-size.sh: docker save %s failed\n' "$image" >&2
    exit 2
fi

mb() { awk -v b="$1" 'BEGIN { printf "%.1f", b / 1000000 }'; }
printf 'compressed: %s MB (budget %s MB)\n' "$(mb "$bytes")" "$(mb "$budget")"
if [ "$bytes" -gt "$budget" ]; then
    printf 'image-size.sh: %s is over budget by %s MB\n' "$image" \
        "$(mb $((bytes - budget)))" >&2
    exit 1
fi
