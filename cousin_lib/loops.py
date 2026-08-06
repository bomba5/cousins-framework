"""The loops daemon: recurring work with one owner.

docs/loops-spec.md is the contract. The structural rule everything
here serves: ONE process owns scheduler state. Requests (manual
fires, edits, timed flips) are rows in a shared store with visible
status - pending, done, failed, expired - because the source's worst
bugs were writes nobody read, living in whichever process happened to
take the call.
"""
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from cousin_lib.config import CousinConfig, FrameworkConfig

REQUEST_TTL_SECONDS = 600
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SCHEDULE_FORMS = ("interval_seconds", "daily_at", "cron")


def _db():
    path = FrameworkConfig.from_env().root / "data" / "loop-requests.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS requests ("
        " id          INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts          REAL NOT NULL,"
        " kind        TEXT NOT NULL,"
        " cousin      TEXT NOT NULL,"
        " payload     TEXT,"
        " ttl_seconds INTEGER NOT NULL,"
        " status      TEXT NOT NULL DEFAULT 'pending',"
        " consumed_at REAL,"
        " reason      TEXT)"
    )
    con.commit()
    return con


def submit_request(kind, *, cousin, payload=None,
                   ttl_seconds=REQUEST_TTL_SECONDS):
    """Write a request row; any process may call this. Returns the id.
    The daemon consumes on its next tick; the row's status is visible
    from the moment it exists."""
    con = _db()
    try:
        cur = con.execute(
            "INSERT INTO requests (ts, kind, cousin, payload,"
            " ttl_seconds) VALUES (?, ?, ?, ?, ?)",
            (time.time(), kind, cousin,
             json.dumps(payload or {}), ttl_seconds),
        )
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def list_requests(*, status=None, limit=100):
    con = _db()
    try:
        where, args = "1=1", []
        if status:
            where, args = "status=?", [status]
        rows = con.execute(
            "SELECT * FROM requests WHERE %s ORDER BY id DESC LIMIT ?"
            % where, args + [limit]).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _finish_request(con, request_id, status, reason=""):
    con.execute(
        "UPDATE requests SET status=?, consumed_at=?, reason=?"
        " WHERE id=?",
        (status, time.time(), reason, request_id))
    con.commit()


def expire_stale_requests(*, now=None):
    """Mark over-TTL pending requests expired. An expired request is a
    LOUD symptom - it means the daemon missed ticks - never a silent
    drop; any reader may run this, so the symptom surfaces even while
    the daemon is down."""
    now = now or time.time()
    con = _db()
    try:
        cur = con.execute(
            "UPDATE requests SET status='expired', consumed_at=?,"
            " reason='daemon missed ticks: request outlived its TTL'"
            " WHERE status='pending' AND ts + ttl_seconds < ?",
            (now, now))
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def _state_path():
    return FrameworkConfig.from_env().root / "data" / "loops-state.json"


def _load_state():
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {"last_tick": None, "last_beat": {}, "last_fires": {}}


def _save_state(state):
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state))
    tmp.replace(path)


def daemon_status(*, tick_interval=30, now=None):
    """The loud-absence contract: every reader of loop state calls
    this and shows the message when ok is false. A daemon that never
    ran and a daemon that stopped are both named, with age."""
    now = now or time.time()
    last_tick = _load_state().get("last_tick")
    if last_tick is None:
        return {"ok": False,
                "message": "loops daemon has never run"}
    age = now - last_tick
    if age > 3 * tick_interval:
        return {"ok": False, "last_tick": last_tick,
                "message": "loops daemon down (last tick %ds ago)"
                           % int(age)}
    return {"ok": True, "last_tick": last_tick, "message": "ok"}


def load_cousin_loops(home):
    """([loops], [errors]). A malformed cousin.toml or invalid loop is
    a reported error NAMING its source - the source framework returned
    an empty list on any parse error, which silently disabled every
    loop the cousin had."""
    import tomllib
    try:
        data = tomllib.loads((Path(home) / "cousin.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        return [], ["cousin.toml unreadable at %s: %s" % (home, err)]
    raw = data.get("loops", [])
    if isinstance(raw, dict):
        raw = [raw]
    loops, errors = [], []
    for entry in raw:
        name = entry.get("name", "")
        if not _NAME_RE.match(name or ""):
            errors.append("loop with invalid name %r in %s"
                          % (name, home))
            continue
        forms = [f for f in _SCHEDULE_FORMS if f in entry]
        if len(forms) != 1:
            errors.append(
                "loop %r in %s must have exactly one schedule form,"
                " has %r" % (name, home, forms))
            continue
        if not (entry.get("prompt") or "").strip():
            errors.append("loop %r in %s has an empty prompt"
                          % (name, home))
            continue
        if not entry.get("enabled", True):  # truthiness, by spec
            continue
        loops.append(entry)
    return loops, errors


def _parse_cron_field(field, minimum, maximum):
    values = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part == "*":
            lo, hi = minimum, maximum
        elif "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(part)
        values.update(range(lo, hi + 1, step))
    return values


def cron_matches(expr, when):
    """Five-field cron. Day-of-month and day-of-week combine with OR
    when both are restricted, as in real cron - a DELIBERATE
    divergence from the source implementation, which ANDed them;
    there are no installed compatibility constraints and
    least-surprise wins while that is true."""
    minute, hour, dom, month, dow = expr.split()
    if when.minute not in _parse_cron_field(minute, 0, 59):
        return False
    if when.hour not in _parse_cron_field(hour, 0, 23):
        return False
    if when.month not in _parse_cron_field(month, 1, 12):
        return False
    dom_set = _parse_cron_field(dom, 1, 31)
    dow_set = {d % 7 for d in _parse_cron_field(dow, 0, 7)}
    dom_restricted = dom != "*"
    dow_restricted = dow != "*"
    dom_ok = when.day in dom_set
    dow_ok = (when.weekday() + 1) % 7 in dow_set
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def _loop_due(loop, last_fire, now):
    when = datetime.fromtimestamp(now)
    if "cron" in loop:
        minute_start = when.replace(second=0, microsecond=0).timestamp()
        return cron_matches(loop["cron"], when) \
            and last_fire < minute_start
    if "daily_at" in loop:
        try:
            hour, minute = map(int, loop["daily_at"].split(":"))
        except ValueError:
            return False
        days = [d[:3].lower() for d in loop.get("days", [])]
        if days and when.strftime("%a").lower() not in days:
            return False
        target = when.replace(hour=hour, minute=minute, second=0,
                              microsecond=0).timestamp()
        last_day = datetime.fromtimestamp(last_fire).date() \
            if last_fire else None
        # Late is better than skipped: no staleness window, one
        # fire per calendar day.
        return now >= target and last_day != when.date()
    interval = int(loop.get("interval_seconds") or 0)
    return interval > 0 and (now - last_fire) >= interval


def _consume_requests(state, deliver, errors):
    con = _db()
    try:
        rows = con.execute(
            "SELECT * FROM requests WHERE status='pending'"
            " ORDER BY id").fetchall()
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            if row["kind"] == "fire":
                slug = row["cousin"]
                home = FrameworkConfig.from_env().root / "cousins" / slug
                loops, errs = load_cousin_loops(home)
                errors.extend(errs)
                target = next(
                    (l for l in loops
                     if l["name"] == payload.get("loop")), None)
                if target is None:
                    _finish_request(con, row["id"], "failed",
                                    "no such loop %r"
                                    % payload.get("loop"))
                    continue
                ok = deliver(slug,
                             "[Framework scheduler: manual fire]\n\n"
                             "### %s\n%s"
                             % (target["name"], target["prompt"]))
                _finish_request(con, row["id"],
                                "done" if ok else "failed",
                                "" if ok else "delivery failed")
            else:
                _finish_request(con, row["id"], "failed",
                                "unknown request kind %r" % row["kind"])
    finally:
        con.close()


def tick(*, deliver, is_alive, now=None):
    """One scheduler tick, per docs/loops-spec.md: per-cousin
    exception isolation, liveness gate, coalesced delivery,
    commit-after-delivery, request consumption, one-shot firing,
    persist. Returns a report."""
    now = now or time.time()
    state = _load_state()
    report = {"fired": [], "errors": [], "requests": 0}
    for config in FrameworkConfig.from_env().list_cousins():
        try:
            slug = config.slug
            if not is_alive(slug):
                continue
            home = FrameworkConfig.from_env().root / "cousins" / slug
            loops, errors = load_cousin_loops(home)
            report["errors"].extend(errors)
            due = []
            for loop in loops:
                key = "%s|%s" % (slug, loop["name"])
                if _loop_due(loop, state["last_fires"].get(key, 0),
                             now):
                    due.append(loop)
            if not due:
                continue
            if len(due) == 1:
                text = due[0]["prompt"]
            else:
                text = ("[Framework scheduler: %d loops due this tick"
                        " - handle in order]\n\n" % len(due)
                        + "\n\n".join("### %s\n%s"
                                      % (l["name"], l["prompt"])
                                      for l in due))
            # Commit-after-delivery: a failed injection leaves every
            # due loop still due.
            if deliver(slug, text):
                for loop in due:
                    key = "%s|%s" % (slug, loop["name"])
                    state["last_fires"][key] = now
                    report["fired"].append(key)
            else:
                report["errors"].append(
                    "delivery failed for %s; loops stay due" % slug)
        except Exception as err:
            # Per-cousin isolation: one flaky cousin never starves
            # the rest of the walk.
            report["errors"].append("%s: %s" % (config.slug, err))
    _consume_requests(state, deliver, report["errors"])
    expire_stale_requests(now=now)
    state["last_tick"] = now
    _save_state(state)
    return report
