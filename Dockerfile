# The framework's image: the source tree at /opt/framework installed in place
# (code finds templates/, hooks/ and pyproject.toml beside the package), the
# sdk extra in /opt/venv, pip removed from the image (the venv's and the base
# image's own), and all state on one volume at /data.
# Build: docker build -t cousins-framework .   Run: docker compose up

FROM python:3.13-slim AS builder
COPY . /opt/framework
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -e "/opt/framework[sdk]" \
 && /opt/venv/bin/pip uninstall -y pip

FROM python:3.13-slim
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
ENV FRAMEWORK_ROOT=/data HOME=/data/home PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /data
VOLUME /data
EXPOSE 8600
# No curl in the slim base: the venv's python asks the public version route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8600/api/version', timeout=4)"]
ENTRYPOINT ["/opt/framework/docker/entrypoint.sh"]
CMD ["cousin-supervisor", "run", "--console-host", "0.0.0.0"]
