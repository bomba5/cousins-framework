"""Host stats and the console's own restart (docs/console-spec.md,
"Host, restart"). Each host block
degrades to zeros on a platform without the /proc files; the restart
never names a service manager."""
from __future__ import annotations

import os
import socket
import threading
import time

from cousin_lib.console import router

RESTART_DELAY_SECONDS = 0.6
_GB = 1024 ** 3


def _read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def _net_bytes():
    rx = tx = 0
    for line in _read("/proc/net/dev").splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr",
                                            "tap", "tun")):
            continue
        cols = rest.split()
        if len(cols) < 16:
            continue
        try:
            rx += int(cols[0])
            tx += int(cols[8])
        except ValueError:
            continue
    return rx, tx


def host_stats(server):
    now = time.time()
    out = {"host": socket.gethostname(), "kernel": "", "uptime": 0}
    try:
        out["uptime"] = int(float(_read("/proc/uptime").split()[0]))
    except (IndexError, ValueError):
        pass
    out["kernel"] = _read("/proc/sys/kernel/osrelease").strip()
    try:
        l1, l5, l15 = os.getloadavg()
        cpus = os.cpu_count() or 1
        out["cpu"] = {"pct": round(min(100.0, l1 * 100.0 / cpus), 1),
                      "load1": l1, "load5": l5, "load15": l15}
    except OSError:
        out["cpu"] = {"pct": 0.0, "load1": 0.0, "load5": 0.0, "load15": 0.0}
    total = avail = cached = 0
    for line in _read("/proc/meminfo").splitlines():
        try:
            if line.startswith("MemTotal:"):
                total = int(line.split()[1])
            elif line.startswith("MemAvailable:"):
                avail = int(line.split()[1])
            elif line.startswith("Cached:"):
                cached = int(line.split()[1])
        except (IndexError, ValueError):
            continue
    out["mem"] = {"total": round(total / 1024 / 1024, 1),
                  "used": round((total - avail) / 1024 / 1024, 1),
                  "cached": round(cached / 1024 / 1024, 1)}
    try:
        st = os.statvfs("/")
        total_gb = st.f_blocks * st.f_frsize / _GB
        free_gb = st.f_bavail * st.f_frsize / _GB
        out["disk"] = {"total": round(total_gb, 1),
                       "used": round(total_gb - free_gb, 1)}
    except OSError:
        out["disk"] = {"total": 0.0, "used": 0.0}
    rx, tx = _net_bytes()
    snap = server.state.get("net_snapshot")
    net = {"rx": 0.0, "tx": 0.0, "rx_total_gb": round(rx / _GB, 2),
           "tx_total_gb": round(tx / _GB, 2)}
    if snap and now > snap["ts"]:
        dt = max(0.1, now - snap["ts"])
        net["rx"] = round(max(0, rx - snap["rx"]) / dt / 1024 / 1024, 2)
        net["tx"] = round(max(0, tx - snap["tx"]) / dt / 1024 / 1024, 2)
    server.state["net_snapshot"] = {"ts": now, "rx": rx, "tx": tx}
    out["net"] = net
    out["console_uptime"] = int(now - server.started_at)
    return out


def register():
    @router.route("GET", "/api/host")
    def host(req):
        return 200, host_stats(req.server)

    @router.route("POST", "/api/admin/restart/framework")
    def restart(req):
        server = req.server
        supervised = bool(os.environ.get("INVOCATION_ID"))
        exit_fn = server.exit_fn or (lambda: os._exit(0))
        timer = threading.Timer(RESTART_DELAY_SECONDS, exit_fn)
        timer.daemon = True
        timer.start()
        return 200, {"ok": True, "target": "console",
                     "supervised": supervised, "eta_seconds": 4}


register()
