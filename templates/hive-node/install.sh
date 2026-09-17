#!/usr/bin/env bash
# Install this hive node on THIS machine. Needs python3 and outbound
# network to the queen, nothing else. Run it from inside the unpacked
# <slug>-node/ directory:
#
#   ./install.sh               install and start a systemd unit
#   ./install.sh --print-unit  render the unit to stdout, touch nothing
#   ./install.sh --foreground  run the node in this shell (no systemd)
#
# It refuses to run without node.env beside it: the env carries the
# queen URL and the bearer token the builder minted, and a node without
# them is not a node.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

if [ ! -f "$HERE/node.env" ]; then
  echo "install.sh: node.env not found beside install.sh; the archive ships one," >&2
  echo "            run this from inside the unpacked <slug>-node/ directory" >&2
  exit 1
fi
set -a
# shellcheck disable=SC1091
. "$HERE/node.env"
set +a

: "${COUSIN_SLUG:?COUSIN_SLUG missing from node.env}"
: "${NODE_PORT:?NODE_PORT missing from node.env}"
: "${QUEEN_URL:?QUEEN_URL missing from node.env}"
: "${HIVE_TOKEN:?HIVE_TOKEN missing from node.env}"

PY="$(command -v python3 || true)"
if [ -z "$PY" ]; then
  echo "install.sh: python3 not found on this machine" >&2
  exit 1
fi

UNIT="cousin-node-${COUSIN_SLUG}.service"

render_unit() {  # $1 = WantedBy target
  cat <<UNITEOF
[Unit]
Description=Hive cousin node: ${COUSIN_SLUG}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${HERE}
EnvironmentFile=${HERE}/node.env
Environment=NODE_DIR=${HERE}
ExecStart=${PY} ${HERE}/cousin_node.py
Restart=always
RestartSec=5

[Install]
WantedBy=${1}
UNITEOF
}

case "${1:-}" in
  --print-unit)
    render_unit multi-user.target
    exit 0
    ;;
  --foreground)
    export NODE_DIR="$HERE"
    exec "$PY" "$HERE/cousin_node.py"
    ;;
  "")
    ;;
  *)
    echo "install.sh: unknown option $1 (--print-unit, --foreground)" >&2
    exit 2
    ;;
esac

if ! command -v systemctl >/dev/null 2>&1; then
  echo "install.sh: no systemctl here; run ./install.sh --foreground under" >&2
  echo "            your own supervisor instead" >&2
  exit 1
fi

echo "installing ${UNIT} (runtime ${HERE}/cousin_node.py, port ${NODE_PORT})"
if sudo -n true 2>/dev/null; then
  echo "-> system unit (passwordless sudo available)"
  TMP="$(mktemp)"
  render_unit multi-user.target > "$TMP"
  sudo cp "$TMP" "/etc/systemd/system/${UNIT}"
  rm -f "$TMP"
  sudo sed -i "/^\[Service\]/a User=$(id -un)" "/etc/systemd/system/${UNIT}"
  sudo systemctl daemon-reload
  sudo systemctl enable --now "${UNIT}"
  CTL="sudo systemctl"
else
  echo "-> user unit (no passwordless sudo); enabling linger so it survives logout"
  mkdir -p "${HOME}/.config/systemd/user"
  render_unit default.target > "${HOME}/.config/systemd/user/${UNIT}"
  loginctl enable-linger "$(id -un)" 2>/dev/null || true
  systemctl --user daemon-reload
  systemctl --user enable --now "${UNIT}"
  CTL="systemctl --user"
fi

echo "waiting for the node to answer on :${NODE_PORT} ..."
for _ in $(seq 1 20); do
  if "$PY" - "$NODE_PORT" <<'PYEOF' 2>/dev/null
import json, sys, urllib.request
with urllib.request.urlopen("http://127.0.0.1:%s/health" % sys.argv[1], timeout=2) as r:
    body = json.loads(r.read())
sys.exit(0 if body.get("status") == "ok" else 1)
PYEOF
  then
    echo "OK - ${COUSIN_SLUG} is up (brain: see /health)."
    echo "logs:  ${CTL} status ${UNIT}   /   journalctl -u ${UNIT} -f"
    echo "stop:  ${CTL} stop ${UNIT}"
    echo "it reaches the queen outbound at ${QUEEN_URL}; nothing dials in."
    exit 0
  fi
  sleep 1
done
echo "install.sh: the node did not answer on :${NODE_PORT} within 20s; check ${CTL} status ${UNIT}" >&2
exit 1
