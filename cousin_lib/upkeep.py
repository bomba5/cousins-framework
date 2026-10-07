"""How much of a cousin's spend goes to keeping itself going (#253).

A turn's cost is in the runner's stream: each `result` event names the
inbox rows the turn answered (`inbox_ids`), and the `usage` event right
after it carries what the turn cost (`cost_usd`, `total` tokens). The
inbox row says why the turn ran (`source`, `thread_id`). So the split is
measured from what the runner already wrote, history included, with no
new bookkeeping.

Each row gets a kind: `heartbeat` (the loops daemon's context beat),
`schedule` (a one-shot the cousin set itself, delivered as a loop item),
`loop` (the cousin's own loops), or the row's source (`chat`, `meeting`,
`boot`, `flip`, `propose`, ...). A turn is upkeep only when every row it
answered is upkeep (`heartbeat`, `boot`, `flip`, `propose`, `interrupt`):
a heartbeat that the operator's message joined is work. A turn with no
rows (a drained or requeued result) or whose rows are gone is `other`.

Costs on a login lane are API-equivalent estimates (usage.db's
`estimate`), the same as the console's tokens page: a share, not a bill.
"""
import json
import sqlite3
import time
from pathlib import Path

UPKEEP_KINDS = ("heartbeat", "boot", "flip", "propose", "interrupt")
WORK_KINDS = ("chat", "meeting", "reaction", "hook", "schedule", "loop")
HEARTBEAT_PREFIX = "Context heartbeat."
SCHEDULE_PREFIX = "[cousin-schedule]"


def row_kind(source, body):
    """A row's kind: the loops daemon's items told apart by their body."""
    if source == "loop":
        text = (body or "").lstrip()
        if text.startswith(HEARTBEAT_PREFIX):
            return "heartbeat"
        if text.startswith(SCHEDULE_PREFIX):
            return "schedule"
        return "loop"
    return source


def _kinds(home):
    """{inbox id: kind} for every row the inbox still holds."""
    path = Path(home) / "data" / "inbox.db"
    if not path.exists():
        return {}
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    try:
        return {i: row_kind(s, b) for i, s, b in
                conn.execute("SELECT id, source, substr(body, 1, 40) FROM inbox")}
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _turns(home, since):
    """(ts, inbox_ids, cost_usd, total tokens) per costed turn since `since`
    (epoch seconds), read from the stream files in time order."""
    out = []
    files = sorted((Path(home) / "data" / "stream").glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime)
    for path in files:
        if path.stat().st_mtime < since:
            continue
        pending = None
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind, payload = ev.get("kind"), ev.get("payload") or {}
            if kind == "result":
                pending = (ev.get("ts") or 0.0, list(payload.get("inbox_ids") or []))
            elif kind == "usage" and pending is not None and "cost_usd" in payload:
                ts, ids = pending
                pending = None
                if ts >= since:
                    out.append((ts, ids, float(payload.get("cost_usd") or 0.0),
                                int(payload.get("total") or 0)))
    return out


def classify(kind):
    if kind in UPKEEP_KINDS:
        return "upkeep"
    if kind in WORK_KINDS:
        return "work"
    return "other"


def turn_kind(kinds):
    """One turn's kind from its rows' kinds: the first work kind if any row
    is work, else the first upkeep kind, else `other`."""
    for want in ("work", "upkeep"):
        for k in kinds:
            if classify(k) == want:
                return k
    return kinds[0] if kinds else "none"


def measure(home, *, days=7, now=None):
    """The split for one cousin over the last `days`: {"turns", "cost_usd",
    "tokens", "classes": {class: {"turns", "cost_usd", "tokens"}},
    "kinds": {kind: {...}}, "upkeep_share": cost share of upkeep, or
    None with no cost}."""
    now = time.time() if now is None else now
    rows = _kinds(home)
    out = {"days": days, "turns": 0, "cost_usd": 0.0, "tokens": 0,
           "classes": {}, "kinds": {}}
    for _ts, ids, cost, tokens in _turns(home, now - days * 86400):
        kind = turn_kind([rows.get(i, "gone") for i in ids])
        for key, name in (("classes", classify(kind)), ("kinds", kind)):
            bucket = out[key].setdefault(name, {"turns": 0, "cost_usd": 0.0, "tokens": 0})
            bucket["turns"] += 1
            bucket["cost_usd"] += cost
            bucket["tokens"] += tokens
        out["turns"] += 1
        out["cost_usd"] += cost
        out["tokens"] += tokens
    spent = out["cost_usd"]
    out["upkeep_share"] = (out["classes"].get("upkeep", {}).get("cost_usd", 0.0) / spent
                           if spent else None)
    return out


def format_measure(slug, m):
    if not m["turns"]:
        return "%s: no costed turns in the last %d days" % (slug, m["days"])
    share = m["upkeep_share"]
    lines = ["%s: %d turns, $%.2f, %d tokens in the last %d days; upkeep %s"
             % (slug, m["turns"], m["cost_usd"], m["tokens"], m["days"],
                "%.0f%%" % (share * 100) if share is not None else "n/a")]
    for name, b in sorted(m["kinds"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        lines.append("  %-10s %-7s %4d turns  $%8.2f  %12d tokens"
                     % (name, classify(name), b["turns"], b["cost_usd"], b["tokens"]))
    return "\n".join(lines)


def upkeep_main(argv=None):
    """cousin-upkeep: each cousin's spend split into upkeep and work."""
    import argparse
    import sys
    from cousin_lib.config import FrameworkConfig, MissingConfigError
    parser = argparse.ArgumentParser(
        prog="cousin-upkeep",
        description="How much of each cousin's spend goes to keeping itself going"
                    " (heartbeats, boots, memory proposals) versus work.")
    parser.add_argument("slug", nargs="*", help="cousins to report (default: every one)")
    parser.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        fw = FrameworkConfig.for_command()
    except MissingConfigError as err:
        print("cousin-upkeep: %s" % err, file=sys.stderr)
        return 2
    cousins = [c for c in fw.list_cousins() if not args.slug or c.slug in args.slug]
    report = {c.slug: measure(c.home, days=max(1, args.days)) for c in cousins}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("\n".join(format_measure(slug, m) for slug, m in report.items()) or "(no cousins)")
    return 0
