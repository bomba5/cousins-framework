"""Health: a record of consecutive failures, per component.

The loops daemon's tick does many things (docs/operations.md, "Health"),
and each one that fails only prints a line to the daemon's log. A thing
that fails on every tick for an hour is then a hundred identical lines
nobody reads. This module keeps the count instead: `<root>/data/health.json`,
one entry per component key, written once per tick by the daemon
(loops.loops_main) from the tick report's `health` list, and by each
runner at its start (its `harness:<slug>` row, runner/main.harness_at_start;
the writers take turns under data/health.json.lock):

    {"<key>": {"state": "ok" | "failing", "fails": <consecutive failures>,
               "since": <ts of the first failure in the current streak, or null>,
               "last_ok": ts | null, "last_fail": ts | null,
               "error": <the last error, one line, at most ERROR_CHARS>,
               "seen": <ts of the last result>}}

An ok result resets the streak. Results for one key within one call are
merged first: any failure makes it a failure (the first error wins), so
a component reported twice in one tick counts once. A component not seen
for STALE_SECONDS (a removed loop, a removed cousin) is pruned at the
next write, so the file stays the size of what runs.

`summary` is what `cousin-health` and the console's `GET /api/health`
show: the store, the failing components first, and the supervisor's
children that are not running (asked the way `cousin-supervisor status`
asks; no supervisor is "not reachable", never a failure)."""
import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ERROR_CHARS = 300
STALE_SECONDS = 7 * 86400
# a failing component whose last result is older than this gets a "not
# seen since" note: its last word stands, but nothing re-checks it now
QUIET_SECONDS = 600
PATH = ("data", "health.json")


def _path(root):
    return Path(root).joinpath(*PATH)


def _one_line(error):
    return " ".join(str(error or "").split())[:ERROR_CHARS]


def read(root):
    """The store as a dict; {} for a missing, unreadable or corrupt file."""
    try:
        data = json.loads(_path(root).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _write(root, data):
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
    os.replace(tmp, path)


@contextlib.contextmanager
def _locked(root):
    """The store's writers in turn: the loops daemon each tick, a runner
    at its start (its `harness:<slug>` row). Each reads, folds and
    writes under one flock, so neither drops the other's row."""
    path = _path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def record(root, results, now=None):
    """Fold one round of results, [(key, ok, error_or_None)], into the
    store and write it (tmp + replace), under the store's lock. Returns
    the new store."""
    with _locked(root):
        return _record(root, results, now)


def _record(root, results, now):
    now = time.time() if now is None else now
    merged = {}
    for key, ok, error in results:
        if key not in merged:
            merged[key] = (bool(ok), None if ok else _one_line(error))
        elif not ok and merged[key][0]:
            merged[key] = (False, _one_line(error))
    data = {k: v for k, v in read(root).items()
            if now - (v.get("seen") or 0) < STALE_SECONDS}
    for key, (ok, error) in merged.items():
        entry = data.get(key) or {"fails": 0, "since": None, "last_ok": None,
                                  "last_fail": None, "error": ""}
        if ok:
            entry.update(state="ok", fails=0, since=None, last_ok=now)
        else:
            if not entry.get("fails"):
                entry["since"] = now
            entry.update(state="failing", fails=int(entry.get("fails") or 0) + 1,
                         last_fail=now, error=error)
        entry["seen"] = now
        data[key] = entry
    _write(root, data)
    return data


def _supervisor(root):
    """{"reachable": bool, "error"?, "failing": [child]}: every supervisor
    child (console, loops, runners, bridges, plugins) not `running`."""
    from cousin_lib import supervisor
    try:
        answer = supervisor.request(root, "status", timeout=3.0)
    except supervisor.SupervisorUnavailable as err:
        return {"reachable": False, "error": str(err), "failing": []}
    failing = []
    for name, row in sorted((answer.get("children") or {}).items()):
        if isinstance(row, dict) and row.get("state") != "running":
            failing.append({"name": name, "state": row.get("state"),
                            "since": row.get("since"), "reason": row.get("reason")})
    return {"reachable": True, "failing": failing}


def summary(root, *, now=None, supervisor=_supervisor):
    """{"components": store, "failing": [row], "ok": [key], "supervisor":
    {...}}; a row is the entry plus its key, the longest streak first."""
    now = time.time() if now is None else now
    data = read(root)
    failing = sorted(({"key": k, **v} for k, v in data.items() if v.get("state") == "failing"),
                     key=lambda r: (-int(r.get("fails") or 0), r["key"]))
    for row in failing:
        row["quiet"] = now - (row.get("seen") or 0) > QUIET_SECONDS
    ok = sorted(k for k, v in data.items() if v.get("state") != "failing")
    return {"components": data, "failing": failing, "ok": ok,
            "supervisor": supervisor(root) if supervisor else None}


def failing_count(body):
    sup = body.get("supervisor") or {}
    return len(body["failing"]) + len(sup.get("failing") or ())


def _when(ts):
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def health_main(argv=None):
    """cousin-health [--all] [--json] [--root R]. Exit codes: 0 nothing
    is failing, 1 something is (a component or a supervisor child), 2
    usage or no root."""
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    parser = argparse.ArgumentParser(
        prog="cousin-health",
        description="what has been failing, and for how long: the loops daemon's per-component"
                    " record (data/health.json) and the supervisor's children",
        epilog="exit status: 0 nothing is failing, 1 something is, 2 bad usage")
    parser.add_argument("--all", action="store_true", help="also list the ok components")
    parser.add_argument("--json", action="store_true", help="print the whole record as JSON")
    parser.add_argument("--root", default=None, help="framework root (else FRAMEWORK_ROOT)")
    args = parser.parse_args(argv)
    try:
        root = FrameworkConfig.resolve(args.root, cwd_fallback=True).root
    except MissingConfigError as err:
        print("cousin-health: %s" % err, file=sys.stderr)
        return 2
    body = summary(root)
    bad = failing_count(body)
    if args.json:
        print(json.dumps(body, indent=1, sort_keys=True))
        return 1 if bad else 0
    for row in body["failing"]:
        note = "  (not seen since %s)" % _when(row.get("seen")) if row["quiet"] else ""
        print("FAIL  %s  %dx since %s  %s%s"
              % (row["key"], row["fails"], _when(row.get("since")), row.get("error") or "", note))
    sup = body["supervisor"]
    for child in sup["failing"]:
        print("FAIL  %s  %s since %s  %s"
              % (child["name"], child["state"], child.get("since") or "-",
                 child.get("reason") or ""))
    if args.all:
        for key in body["ok"]:
            entry = body["components"][key]
            print("ok    %s  last ok %s" % (key, _when(entry.get("last_ok"))))
    print("%d ok, %d failing" % (len(body["ok"]), bad))
    if not sup["reachable"]:
        print("supervisor not reachable: %s" % sup.get("error"))
    if not body["components"]:
        print("(no record yet: the loops daemon writes %s on each tick)" % _path(root))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(health_main())
