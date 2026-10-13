#!/bin/sh
# Docker Hub limits anonymous pulls per IP, and a hosted runner shares its
# IP: the image and quick start jobs failed on 429 toomanyrequests for the
# python base (#303). This points the runner's Docker daemon, builds
# included, at Google's mirror of Docker Hub: the same official images by
# the same digests, no account and no secret. For an image the mirror does
# not hold the daemon falls back to Docker Hub. CI only: the Dockerfile,
# the compose files and the README stay on docker.io.
set -eu
conf=/etc/docker/daemon.json
mirror=https://mirror.gcr.io
sudo python3 - "$conf" "$mirror" <<'EOF'
import json, os, sys
path, mirror = sys.argv[1:]
conf = {}
if os.path.exists(path):
    with open(path) as f:
        conf = json.load(f)
conf["registry-mirrors"] = [mirror]
with open(path + ".tmp", "w") as f:
    json.dump(conf, f, indent=2)
os.replace(path + ".tmp", path)
EOF
sudo systemctl restart docker
got=$(docker info --format '{{range .RegistryConfig.Mirrors}}{{.}} {{end}}')
case "$got" in
    *"$mirror"*) echo "docker.io pulls go through $got" ;;
    *) echo "the daemon did not take the mirror: '$got'" >&2; exit 1 ;;
esac
