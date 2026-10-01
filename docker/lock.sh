#!/bin/sh
# docker/lock.sh
#
# Re-resolves docker/requirements.txt, the image's Python lock: every
# package the image installs (the sdk extra and what it pulls, plus the
# build backend the editable install runs without build isolation), each
# at one exact version with the sha256 of every file PyPI has for it, so
# an arm64 build finds its wheels' hashes too. pyproject.toml's ranges
# are the input and stay ranges for pip users.
#
# The resolve runs in the Dockerfile's own pinned base image (the
# PYTHON_IMAGE default), so it sees the image's Python and platform. It
# needs docker and the network, writes nothing but docker/requirements.txt
# and leaves no container behind. Run it from anywhere in a checkout:
#
#   sh docker/lock.sh
#
# then rebuild the image and commit the lock with the change that needed
# it (a new range in pyproject.toml, a security update, a new base image).
set -eu

PIP_TOOLS=7.6.1

repo=$(cd "$(dirname "$0")/.." && pwd)
image=$(sed -n 's/^ARG PYTHON_IMAGE=//p' "$repo/Dockerfile")
if [ -z "$image" ]; then
    printf 'lock.sh: no ARG PYTHON_IMAGE=... line in %s/Dockerfile\n' "$repo" >&2
    exit 2
fi

out=$(mktemp)
trap 'rm -f "$out"' EXIT
# The checkout is mounted read-only and the project copied out of it:
# reading the metadata builds an egg-info that must not land in the tree.
docker run --rm -v "$repo:/src:ro" "$image" sh -c '
    set -eu
    mkdir /tmp/project
    cp -r /src/pyproject.toml /src/README.md /src/LICENSE /src/cousin_lib /tmp/project/
    cd /tmp/project
    pip install --quiet --no-cache-dir --root-user-action=ignore \
        --disable-pip-version-check "pip-tools=='"$PIP_TOOLS"'" >&2
    pip-compile --quiet --no-header --no-emit-index-url --strip-extras \
        --extra sdk --all-build-deps --allow-unsafe --generate-hashes \
        --output-file /tmp/requirements.txt pyproject.toml \
        >/tmp/compile.log 2>&1 || { cat /tmp/compile.log >&2; exit 1; }
    cat /tmp/requirements.txt
' >"$out"

{
    printf '%s\n' \
        '# The image'"'"'s Python lock, written by docker/lock.sh: do not edit by hand.' \
        '# Resolved in '"$image" \
        '# with pip-tools '"$PIP_TOOLS"' from pyproject.toml (extra sdk, build deps).' \
        '# The Dockerfile installs it with --require-hashes.'
    cat "$out"
} >"$repo/docker/requirements.txt"
printf 'wrote docker/requirements.txt (%s pins)\n' \
    "$(grep -c '^[A-Za-z0-9]' "$repo/docker/requirements.txt")"
