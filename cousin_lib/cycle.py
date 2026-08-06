"""Cycle counters: a cousin's own session-cadence breadcrumbs.

Counters and short breadcrumbs at <home>/data/cycle.json - semantic
state belongs in the cousin's memory files, not here. The file's one
framework consumer is boot's staleness check, which reads only its
MTIME; everything else is the cousin narrating to itself. The source
carried two more fields (overlay, milestone) with zero readers
anywhere; they do not ship.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

HISTORY_MAX = 50


def _now():
    return datetime.now().replace(microsecond=0).isoformat()


class _NoContext(Exception):
    pass


def _home():
    home = os.environ.get("COUSIN_HOME")
    if not home:
        raise _NoContext("cousin-cycle: set COUSIN_HOME")
    return Path(home)


def _path(home):
    return home / "data" / "cycle.json"


def _init():
    return {"cycle_id": 0, "started_at": None, "ended_at": None,
            "last_action": None, "event_count": 0,
            "last_completed_at": None, "history": []}


def _load(home):
    try:
        return json.loads(_path(home).read_text())
    except (OSError, ValueError):
        return _init()


def _save(home, state):
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(path)


def cycle_main(argv=None):
    parser = argparse.ArgumentParser(prog="cousin-cycle")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("state")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("inc")
    p.add_argument("--start", action="store_true")
    p.add_argument("--end", action="store_true")
    p.add_argument("--action")
    sub.add_parser("reset")
    args = parser.parse_args(argv)
    try:
        home = _home()
    except _NoContext as err:
        print(str(err), file=sys.stderr)
        return 2
    state = _load(home)
    if args.cmd == "state":
        if args.json:
            print(json.dumps(state, indent=1))
        else:
            print("cycle %d, %d event(s), last: %s"
                  % (state["cycle_id"], state["event_count"],
                     state["last_action"] or "(none)"))
        return 0
    if args.cmd == "reset":
        # Archive, never delete: the breadcrumbs survive the reset.
        archive_path = home / "data" / "cycle-archive.json"
        try:
            archive = json.loads(archive_path.read_text())
        except (OSError, ValueError):
            archive = []
        archive.append(state)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = archive_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(archive, indent=1))
        tmp.replace(archive_path)
        _save(home, _init())
        print("cycle state archived and reset")
        return 0
    # inc
    if args.start:
        state["cycle_id"] += 1
        state["started_at"] = _now()
        state["ended_at"] = None
    if args.action:
        state["last_action"] = args.action
        state["event_count"] += 1
        state["history"] = (state["history"]
                            + [{"at": _now(),
                                "action": args.action}])[-HISTORY_MAX:]
    if args.end:
        state["ended_at"] = _now()
        state["last_completed_at"] = _now()
    _save(home, state)
    print("cycle %d: %d event(s)" % (state["cycle_id"],
                                     state["event_count"]))
    return 0


if __name__ == "__main__":
    sys.exit(cycle_main())
