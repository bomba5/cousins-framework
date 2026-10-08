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
`boot`, `flip`, `propose`, ...). A turn with no row that the SDK started
when a background task finished is `task`. Four classes:
- upkeep: every row is `heartbeat`, `boot`, `flip` or `propose`;
- self: the turn answered a `schedule`, a prompt the cousin set itself,
  or a `job` close notice it asked for (`cousin-job start --notify`);
  the tool cannot tell a self-set heartbeat from a reminder, so it is
  shown apart, neither upkeep nor work: upkeep is a floor, upkeep + self
  a ceiling;
- work: any row is `chat`, `meeting`, `reaction`, `hook`, `loop` or
  `interrupt` (the operator stopping a turn), or the turn is a `task` (a
  heartbeat that the operator's message joined is work);
- other: a row that is gone from the inbox, a source in no list (today
  only `outbox`, the report of how a message to another install ended),
  or a turn with no row that is not a `task`.

Only the sdk and opencode runners write `usage` events; a tmux cousin
always reads as no costed turns.

Costs on a login lane are API-equivalent estimates (usage.db's
`estimate`), the same as the console's tokens page: a share, not a bill.
"""
import json
import sqlite3
import time
from pathlib import Path

UPKEEP_KINDS = ("heartbeat", "boot", "flip", "propose")
SELF_KINDS = ("schedule", "job")   # a job's close notice: a wake-up the cousin asked for
# an interrupt is the operator stopping a turn: their act, not upkeep
WORK_KINDS = ("chat", "meeting", "reaction", "hook", "loop", "task", "interrupt")
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


def generation_idle(home, since):
    """True when every inbox row since `since` (the generation's start)
    is upkeep: heartbeats, the boot, a flip, memory proposals. Nothing
    asked the cousin for work and it set itself nothing, so its daily
    flip would buy only a handoff and a boot (#280). False when the inbox
    is missing or unreadable: flipping is the safe default."""
    path = Path(home) / "data" / "inbox.db"
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    except sqlite3.Error:
        return False
    try:
        rows = conn.execute("SELECT source, substr(body, 1, 40) FROM inbox"
                            " WHERE created_at >= ?", (float(since),)).fetchall()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return all(classify(row_kind(source, body)) == "upkeep" for source, body in rows)


_CACHE = {}          # (path, mtime, size) -> the file's costed turns


def _file_turns(path):
    """(ts, inbox_ids, background, cost_usd, total tokens) per costed turn
    in one stream file, cached by its mtime and size: a file only grows,
    and the console asks for every cousin on each Tokens page load."""
    try:
        st = path.stat()
    except OSError:
        return []
    key = (str(path), st.st_mtime, st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    out, pending, notified = [], None, False
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        kind, payload = ev.get("kind"), ev.get("payload") or {}
        if kind == "system" and payload.get("subtype") == "task_notification":
            notified = True
        elif kind == "result":
            ids = list(payload.get("inbox_ids") or [])
            # Streams written before the runner set `background` carry
            # only the task_notification that woke the turn.
            pending = (ev.get("ts") or 0.0, ids,
                       bool(payload.get("background")) or (notified and not ids))
            notified = False
        elif kind == "usage" and pending is not None and "cost_usd" in payload:
            out.append(pending + (float(payload.get("cost_usd") or 0.0),
                                  int(payload.get("total") or 0)))
            pending = None
    for old in [k for k in _CACHE if k[0] == key[0]]:
        del _CACHE[old]
    _CACHE[key] = out
    return out


def _turns(home, since):
    """(ts, inbox_ids, background, cost_usd, total tokens) per costed turn
    since `since` (epoch seconds), from the stream files in time order."""
    out = []
    files = sorted((Path(home) / "data" / "stream").glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime)
    for path in files:
        if path.stat().st_mtime < since:
            continue
        out.extend(t for t in _file_turns(path) if t[0] >= since)
    return out


def classify(kind):
    if kind in UPKEEP_KINDS:
        return "upkeep"
    if kind in SELF_KINDS:
        return "self"
    if kind in WORK_KINDS:
        return "work"
    return "other"


def turn_kind(kinds, background=False):
    """One turn's kind from its rows' kinds: the first work kind if any row
    is work, else a self kind, else an upkeep kind, else the first row's
    kind; a turn with no rows is `task` when the SDK started it for a
    finished background task, `none` otherwise."""
    if not kinds:
        return "task" if background else "none"
    for want in ("work", "self", "upkeep"):
        for k in kinds:
            if classify(k) == want:
                return k
    return kinds[0]


def measure(home, *, days=7, now=None):
    """The split for one cousin over the last `days`: {"days", "turns",
    "cost_usd", "tokens" (usage totals, cache reads included), "classes":
    {upkeep | self | work | other: {"turns", "cost_usd", "tokens"}},
    "kinds": {kind: {...}}, "upkeep_share" (upkeep's share of the cost, a
    floor) and "upkeep_or_self_share" (upkeep + self, a ceiling), both
    None with no cost}."""
    now = time.time() if now is None else now
    rows = _kinds(home)
    out = {"days": days, "turns": 0, "cost_usd": 0.0, "tokens": 0,
           "classes": {}, "kinds": {}}
    for _ts, ids, background, cost, tokens in _turns(home, now - days * 86400):
        kind = turn_kind([rows.get(i, "gone") for i in ids], background)
        for key, name in (("classes", classify(kind)), ("kinds", kind)):
            bucket = out[key].setdefault(name, {"turns": 0, "cost_usd": 0.0, "tokens": 0})
            bucket["turns"] += 1
            bucket["cost_usd"] += cost
            bucket["tokens"] += tokens
        out["turns"] += 1
        out["cost_usd"] += cost
        out["tokens"] += tokens
    spent = out["cost_usd"]
    cost = lambda c: out["classes"].get(c, {}).get("cost_usd", 0.0)
    out["upkeep_share"] = cost("upkeep") / spent if spent else None
    out["upkeep_or_self_share"] = (cost("upkeep") + cost("self")) / spent if spent else None
    return out


def format_measure(slug, m):
    if not m["turns"]:
        return "%s: no costed turns in the last %d days" % (slug, m["days"])
    pct = lambda v: "%.0f%%" % (v * 100) if v is not None else "n/a"
    lines = ["%s: %d turns, $%.2f in the last %d days; upkeep %s (with its own schedules %s)"
             % (slug, m["turns"], m["cost_usd"], m["days"], pct(m["upkeep_share"]),
                pct(m["upkeep_or_self_share"]))]
    for name, b in sorted(m["kinds"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        lines.append("  %-10s %-7s %4d turns  $%8.2f  %12d tokens (cache reads included)"
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
    parser.add_argument("--days", type=int, default=7, help="window in days, 1-90 (default 7)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        fw = FrameworkConfig.for_command()
    except MissingConfigError as err:
        print("cousin-upkeep: %s" % err, file=sys.stderr)
        return 2
    cousins = [c for c in fw.list_cousins() if not args.slug or c.slug in args.slug]
    if not 1 <= args.days <= 90:
        print("cousin-upkeep: --days must be 1-90", file=sys.stderr)
        return 2
    report = {c.slug: measure(c.home, days=args.days) for c in cousins}
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("\n".join(format_measure(slug, m) for slug, m in report.items()) or "(no cousins)")
    return 0
