# The framework's image: the source tree at /opt/framework installed in place
# (code finds templates/, hooks/ and pyproject.toml beside the package), the
# sdk extra in /opt/venv, pip removed from the image (the venv's and the base
# image's own), and all state on one volume at /data.
# Build: docker build -t cousins-framework .   Run: docker compose up
# A build without --target is the default image (the last stage): the
# framework plus the pinned opencode binary, so an opencode cousin runs
# with no extra step; still no node and no bun. `--target opencode` names
# the same image (its name before it became the default). `--target slim`
# is the image without opencode, for a Claude-only install that wants the
# smaller image (compose.slim.yml).
#
# The inputs are pinned: the base image by its multi-arch index digest (the
# tag is kept for the reader; a build resolves the digest only), the Python
# packages by docker/requirements.txt, exact versions with their sha256
# (docker/lock.sh re-resolves it), the opencode binary by its sha256 below.
# No stage installs a system package. Refreshing the base: put the index
# digest of `docker buildx imagetools inspect python:3.13-slim` here, then
# run docker/lock.sh.
ARG PYTHON_IMAGE=python:3.13-slim@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b

# The lock first, with hashes required (a package missing from it, or a
# file whose hash differs, stops the build), then the framework itself in
# place with no dependency resolution and the locked build backend instead
# of a freshly downloaded one; pip check holds the result together. The
# backend and pip leave the venv after: nothing at run time needs them.
FROM ${PYTHON_IMAGE} AS builder
COPY . /opt/framework
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --require-hashes \
      -r /opt/framework/docker/requirements.txt \
 && /opt/venv/bin/pip install --no-cache-dir --no-deps --no-build-isolation \
      -e "/opt/framework[sdk]" \
 && /opt/venv/bin/pip check \
 && /opt/venv/bin/pip uninstall -y setuptools pip

FROM ${PYTHON_IMAGE} AS final
# One unprivileged user; its home is on the volume, where a fresh agent
# session writes its transcript (a cache the framework never reads). The
# base image's own pip goes too: nothing at run time installs packages
# (without bytecode: the .pyc files pip's run would leave cost 4 MB).
RUN groupadd --gid 10001 cousin \
 && useradd --uid 10001 --gid 10001 --home-dir /data/home --no-create-home \
      --shell /bin/bash cousin \
 && mkdir /data \
 && chown 10001:10001 /data \
 && PYTHONDONTWRITEBYTECODE=1 python3 -m pip uninstall -y pip
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/framework /opt/framework
# COUSIN_IN_CONTAINER=1 tells the framework it runs in this image: a
# login line for `host` then names the compose exec on the Docker host,
# not a host user on the container's id.
ENV FRAMEWORK_ROOT=/data HOME=/data/home PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 \
    COUSIN_IN_CONTAINER=1
USER 10001:10001
WORKDIR /data
VOLUME /data
EXPOSE 8600
# No curl in the slim base: the venv's python asks the public version route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8600/api/version', timeout=4)"]
ENTRYPOINT ["/opt/framework/docker/entrypoint.sh"]
CMD ["cousin-supervisor", "run", "--console-host", "0.0.0.0"]

# The slim image: final alone, without opencode (docker build --target
# slim, compose.slim.yml). No later stage builds from it; it is a name.
FROM final AS slim

# The opencode binary, for the default image. The pinned opencode release
# is one self-contained binary (a compiled Bun executable: no node, no bun,
# no npm at run time). It comes from the npm registry's platform package,
# downloaded by this throwaway stage and checked twice: the tarball's
# sha256, then the binary's. The pins were computed once from the
# registry's file, whose sha512 matched the registry's dist.integrity.
# Only amd64 is pinned; another architecture needs its own package name
# and shas here.
FROM ${PYTHON_IMAGE} AS opencode-fetch
ARG TARGETARCH
RUN set -eu; \
    version=1.18.31; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) pkg=opencode-linux-x64; \
             tgz_sha256=6d89da252a8b030d923e728396dc34465cf6095101b78222b0ee337b68140dea; \
             bin_sha256=f9dab32248695e9ebd56b16a1921798fd85112cf5a69c7dfd0cabc1e17be4a11; ;; \
      *) echo "opencode: no pinned build for $arch" >&2; exit 1 ;; \
    esac; \
    url="https://registry.npmjs.org/$pkg/-/$pkg-$version.tgz"; \
    python3 -c 'import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' \
      "$url" /tmp/opencode.tgz; \
    echo "$tgz_sha256  /tmp/opencode.tgz" | sha256sum -c -; \
    tar -xzf /tmp/opencode.tgz -C /tmp package/bin/opencode; \
    echo "$bin_sha256  /tmp/package/bin/opencode" | sha256sum -c -; \
    install -D -m 0755 /tmp/package/bin/opencode /opt/opencode/bin/opencode; \
    rm -rf /tmp/opencode.tgz /tmp/package

# The slim image plus the binary, owned by root (the cousin user cannot
# replace it). The runner finds it on PATH or through COUSIN_OPENCODE_BIN.
FROM final AS opencode
COPY --from=opencode-fetch /opt/opencode /opt/opencode
ENV COUSIN_OPENCODE_BIN=/opt/opencode/bin/opencode PATH=/opt/opencode/bin:$PATH

# The default target: docker build (no --target) builds the last stage,
# and this one is the opencode stage, unchanged.
FROM opencode AS default
