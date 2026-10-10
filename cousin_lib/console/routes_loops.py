"""Loops routes (docs/reference/console-api.md, "Loops"): rows from every
cousin's [[loops]] plus a synthetic context-heartbeat row per
non-worker cousin, the daemon status on every response, recent fires,
drift from the daemon's fire log, the per-cousin editor, and fire as a
request row the daemon consumes."""
from __future__ import annotations

import time

from cousin_lib import jobs, loops
from cousin_lib.config import FrameworkConfig
from cousin_lib.console import router
from cousin_lib.console._common import check_name, check_slug, cousin_home
from cousin_lib.console.app import HttpError

BEAT = "context-heartbeat"
DRIFT_POINTS = 80


def _fires_by_key(rows):
    out = {}
    for row in rows:
        key = "%s|%s" % (row.get("cousin"), row.get("loop"))
        try:
            out.setdefault(key, []).append(float(row["ts"]))
        except (TypeError, ValueError):
            continue
    for series in out.values():
        series.sort()
    return out


def _worker_failed(slug, key):
    for job in jobs.list_jobs(spawned_by=slug, limit=50):
        if job.get("title") == "worker %s" % key:
            return job.get("status") == "failed"
    return False


def _row(config, entry, state, now, fires):
    key = "%s|%s" % (config.slug, entry["name"])
    last = float(state.get("last_fires", {}).get(key, 0) or 0)
    enabled = bool(entry.get("enabled", True))
    interval = int(entry.get("interval_seconds") or 0)
    if not enabled:
        loop_state = "disabled"
    elif config.type == "worker" and _worker_failed(config.slug, key):
        loop_state = "failed"
    else:
        loop_state = "healthy" if last else "idle"
    series = fires.get(key, [])
    drift = 0
    if interval and len(series) >= 2:
        drift = max(0, int((series[-1] - series[-2]) - interval))
    return {
        "cousin": config.slug,
        "name": entry["name"],
        "state": loop_state,
        "interval": interval,
        "schedule": {"interval_seconds": interval,
                     "daily_at": entry.get("daily_at", ""),
                     "cron": entry.get("cron", ""),
                     "days": list(entry.get("days", []) or [])},
        "prompt": entry.get("prompt", ""),
        "enabled": enabled,
        "hidden": bool(entry.get("hidden", False)),
        "lastFireTs": int(last),
        "lastTick": int(now - last) if last else 0,
        "nextFireTs": loops.next_due(entry, last, now=now) if enabled else 0,
        "drift": drift,
        "note": (entry.get("prompt") or "")[:60],
        "source": "framework",
    }


def _beat_row(config, state, now):
    last = float(state.get("last_beat", {}).get(config.slug, 0) or 0)
    checked = float((state.get("beat_checked") or {}).get(config.slug, 0) or 0)
    interval = int(config.heartbeat_seconds or 0)
    # due, and the daemon's last look found nothing new: it beats at the
    # first tick after a change, so there is no time to count down to (#305)
    quiet = interval > 0 and checked > last and now - last >= interval
    return {
        "cousin": config.slug,
        "name": BEAT,
        "state": "disabled" if interval <= 0 else
                 ("quiet" if quiet else "healthy" if last else "idle"),
        "interval": interval,
        "schedule": {"interval_seconds": interval, "daily_at": "",
                     "cron": "", "days": []},
        "prompt": "",
        "enabled": interval > 0,
        "hidden": False,
        "lastFireTs": int(last),
        "lastTick": int(now - last) if last else 0,
        "nextFireTs": (int(max(now, last + interval)) if interval > 0 and not quiet
                       else 0),
        "drift": 0,
        "note": ("context heartbeat: quiet, nothing changed since the last beat;"
                 " it beats on the next change" if quiet
                 else "context heartbeat (the daemon's own beat)"),
        "source": "framework",
    }


def loops_rows(server, *, now=None):
    now = now or time.time()
    state = loops._load_state()
    fires = _fires_by_key(loops.read_fires())
    rows, errors = [], []
    for config in FrameworkConfig(server.root).list_cousins():
        entries, errs = loops.load_cousin_loops(config.home,
                                                include_disabled=True)
        errors.extend(errs)
        if config.type != "worker":
            rows.append(_beat_row(config, state, now))
        for entry in entries:
            rows.append(_row(config, entry, state, now, fires))
    return {"loops": rows, "errors": errors}


def _cousin_entries(server, slug):
    """Every [[loops]] entry of one cousin; a file with invalid entries
    is refused for editing, because a save would silently drop them."""
    home = cousin_home(server, slug)
    entries, errors = loops.load_cousin_loops(home, include_disabled=True)
    return home, entries, errors


def register():
    @router.route("GET", "/api/loops")
    def all_loops(req):
        out = loops_rows(req.server)
        out["daemon"] = loops.daemon_status()
        return 200, out

    @router.route("GET", "/api/loops/recent")
    def recent(req):
        now = time.time()
        state = loops._load_state()
        types = {c.slug: c.type
                 for c in FrameworkConfig(req.server.root).list_cousins()}
        fires = []
        for slug, ts in (state.get("last_beat") or {}).items():
            if types.get(slug) == "worker":
                continue
            fires.append((float(ts or 0), slug, BEAT))
        for key, ts in (state.get("last_fires") or {}).items():
            slug, _, name = key.partition("|")
            fires.append((float(ts or 0), slug, name))
        fires.sort(reverse=True)
        return 200, {"fires": [{"cousin": slug, "loop": name,
                                "ago": int(now - ts)}
                               for ts, slug, name in fires[:20]],
                     "daemon": loops.daemon_status()}

    @router.route("GET", "/api/loops/drift/{slug}/{name}")
    def drift(req, slug, name):
        check_slug(slug)
        check_name(name)
        home = cousin_home(req.server, slug)
        entries, _errors = loops.load_cousin_loops(home, include_disabled=True)
        interval = 0
        for entry in entries:
            if entry["name"] == name:
                interval = int(entry.get("interval_seconds") or 0)
        series = _fires_by_key(loops.read_fires()).get(
            "%s|%s" % (slug, name), [])
        points = []
        for prev, cur in zip(series, series[1:]):
            gap = int(cur - prev)
            points.append({"t": int(cur), "interval": gap,
                           "drift": max(0, gap - interval) if interval else 0})
        points = points[-DRIFT_POINTS:]
        return 200, {"ok": True, "slug": slug, "name": name,
                     "interval": interval, "n": len(points), "points": points}

    @router.route("GET", "/api/cousins/{slug}/loops")
    def cousin_loops(req, slug):
        _home, entries, errors = _cousin_entries(req.server, slug)
        state = loops._load_state()
        prefix = slug + "|"
        out = {"loops": entries,
               "last_beat": int(float(
                   (state.get("last_beat") or {}).get(slug, 0) or 0)),
               "last_fires": {k[len(prefix):]: v for k, v in
                              (state.get("last_fires") or {}).items()
                              if k.startswith(prefix)},
               "daemon": loops.daemon_status()}
        if errors:
            out["errors"] = errors
        return 200, out

    @router.route("POST", "/api/cousins/{slug}/loops")
    def save(req, slug):
        home = cousin_home(req.server, slug)
        entries = req.body.get("loops")
        if not isinstance(entries, list):
            raise HttpError(400, "loops must be a list")
        try:
            saved = loops.save_cousin_loops(home, entries)
        except ValueError as err:
            raise HttpError(400, str(err))
        req.server.emit("loops-refresh", loops_rows(req.server)["loops"])
        return 200, {"ok": True, "slug": slug, "loops": saved}

    @router.route("POST", "/api/cousins/{slug}/loops/{name}/hidden")
    def hidden(req, slug, name):
        check_name(name)
        home, entries, errors = _cousin_entries(req.server, slug)
        if errors:
            raise HttpError(400, "cousin.toml has invalid loops; fix them"
                                 " by hand first: %s" % "; ".join(errors))
        flag = req.body.get("hidden")
        if not isinstance(flag, bool):
            raise HttpError(400, "hidden must be a boolean")
        target = next((e for e in entries if e["name"] == name), None)
        if target is None:
            raise HttpError(404, "unknown loop %s" % name)
        if flag:
            target["hidden"] = True
        else:
            target.pop("hidden", None)
        saved = loops.save_cousin_loops(home, entries)
        return 200, {"ok": True, "slug": slug, "loops": saved}

    @router.route("POST", "/api/cousins/{slug}/loops/{name}/fire")
    def fire(req, slug, name):
        cousin_home(req.server, slug)
        check_name(name)
        request_id = loops.submit_request("fire", cousin=slug,
                                          payload={"loop": name})
        return 202, {"ok": True, "slug": slug, "name": name,
                     "request_id": request_id}


register()
