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


_BEAT_FILES = ("CLAUDE.md", "STATUS.md", "MEMORY.md")
_BEAT_INLINE_CAP = 6000


def _compose_beat(home, now):
    """(prompt, commit) for the context beat, or (None, None) when
    composition fails. The mtime state is captured here but WRITTEN
    only by commit() - which the tick calls after delivery succeeded.
    The source wrote state before injecting; a failed inject lost the
    delta and the next beat reported 'no changes' over real ones."""
    home = Path(home)
    state_path = home / "data" / "heartbeat-mtimes.json"
    try:
        seen = json.loads(state_path.read_text())
    except (OSError, ValueError):
        seen = {}
    changed, current = [], {}
    for name in _BEAT_FILES:
        path = home / name
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        current[name] = mtime
        if seen.get(name) != mtime:
            body = path.read_text(errors="replace")[:_BEAT_INLINE_CAP]
            changed.append(
                "--- %s CHANGED since last heartbeat (%s) ---\n%s\n"
                "--- end %s ---" % (name, path, body, name))
    if changed:
        delta = ("These are the AUTHORITATIVE current contents:\n\n"
                 + "\n\n".join(changed))
    else:
        delta = ("No identity files changed since the last heartbeat;"
                 " use cousin-memory search for anything older.")
    prompt = (
        "Context heartbeat. %s\n\nThen run cousin-memory activity"
        " \"<brief current state>\" to checkpoint; cousin-memory"
        " decide only if something non-trivial changed. Finally emit"
        " one line 'Heartbeat at HH:MM'." % delta)

    def commit():
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current))
        tmp.replace(state_path)

    return prompt, commit


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


def _fire_worker_loops(config, state, now, report):
    """A worker cousin has no session and no beats; a due loop runs
    the worker command template from host configuration as a tracked
    background job. The EXIT CODE lands in the job row (the jobs
    module's detached runner writes it back), so a worker failing
    every firing looks failed everywhere loop state is shown - the
    source marked fires successful before the subprocess ran. With no
    worker-cmd configured the loop STAYS DUE and the error names the
    remediation."""
    import shlex

    from cousin_lib import jobs

    root = FrameworkConfig.from_env().root
    home = root / "cousins" / config.slug
    loops, errors = load_cousin_loops(home)
    report["errors"].extend(errors)
    try:
        template = (root / "config" / "worker-cmd").read_text().strip()
    except OSError:
        template = ""
    for loop in loops:
        key = "%s|%s" % (config.slug, loop["name"])
        if not _loop_due(loop, state["last_fires"].get(key, 0), now):
            continue
        if not template:
            report["errors"].append(
                "worker loop %s due but no worker command configured;"
                " write config/worker-cmd (loop stays due)" % key)
            continue
        cmd = [part.replace("{prompt}", loop["prompt"])
                   .replace("{home}", str(home))
               for part in shlex.split(template)]
        job_id = jobs.register_job(
            kind="other", title="worker %s" % key,
            spawned_by=config.slug, command=" ".join(cmd))
        log_path = jobs._default_log_path(job_id)
        jobs.set_log_path(job_id, str(log_path))
        jobs._spawn_tracked(cmd, log_path, job_id)
        # The RUN is the firing; the rc arrives in the job row when
        # the detached runner finishes.
        state["last_fires"][key] = now
        report["fired"].append(key)


def _consume_requests(state, deliver, errors):
    con = _db()
    try:
        # kind='flip' rows belong to the timed-flip walker, which
        # holds them pending until T-0; consuming them here would
        # mark them failed-unknown before their time.
        rows = con.execute(
            "SELECT * FROM requests WHERE status='pending'"
            " AND kind != 'flip' ORDER BY id").fetchall()
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


_WARN_LADDER = (
    (300, "wrap up tool calls - flip in 5 minutes"),
    (60, "finalize your handoff now - flip in 1 minute"),
    (30, "write data/handoff.md - flip in 30 seconds"),
)


def _walk_timed_flips(state, deliver, do_flip, now, report):
    """Timed flips live in the request store - which is what makes
    them actually fire: the source scheduled them in one process and
    walked an always-empty dict in the other. Warnings at T-5m/1m/30s,
    fire at T-0, done/failed on the row."""
    con = _db()
    try:
        rows = con.execute(
            "SELECT * FROM requests WHERE status='pending'"
            " AND kind='flip' ORDER BY id").fetchall()
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            fire_at = float(payload.get("fire_at", 0))
            slug = row["cousin"]
            if now < fire_at:
                warns = state.setdefault("timed_warns", {}) \
                    .setdefault(str(row["id"]), [])
                for threshold, text in _WARN_LADDER:
                    key = str(threshold)
                    if key not in warns and fire_at - now <= threshold:
                        deliver(slug, "[cousin-flip] %s" % text)
                        warns.append(key)
                continue
            result = do_flip(slug)
            if result.get("ok"):
                _finish_request(con, row["id"], "done")
            else:
                _finish_request(con, row["id"], "failed",
                                result.get("error", "flip failed"))
            state.get("timed_warns", {}).pop(str(row["id"]), None)
            report["flips"].append(slug)
    finally:
        con.close()


def _fire_daily_flips(state, do_flip, now, report):
    """flip_at drivers: late-once per day, and AT MOST ONE flip per
    tick - the tick cadence is the stagger that keeps boot packets
    from assembling simultaneously."""
    if report["flips"]:
        return  # a timed flip already used this tick's slot
    when = datetime.fromtimestamp(now)
    for config in FrameworkConfig.from_env().list_cousins():
        if not config.flip_at or config.type == "worker":
            continue
        try:
            hour, minute = map(int, config.flip_at.split(":"))
        except ValueError:
            report["errors"].append(
                "unparsable flip_at %r for %s"
                % (config.flip_at, config.slug))
            continue
        target = when.replace(hour=hour, minute=minute, second=0,
                              microsecond=0).timestamp()
        last = state.setdefault("last_flips", {}).get(config.slug)
        if now >= target and last != str(when.date()):
            result = do_flip(config.slug)
            state["last_flips"][config.slug] = str(when.date())
            report["flips"].append(config.slug)
            if not result.get("ok"):
                report["errors"].append(
                    "daily flip failed for %s: %s"
                    % (config.slug, result.get("error", "?")))
            return  # one per tick


def _default_do_flip(slug):
    from cousin_lib.flip import flip
    return flip(slug)


def tick(*, deliver, is_alive, now=None, do_flip=_default_do_flip):
    """One scheduler tick, per docs/loops-spec.md: per-cousin
    exception isolation, liveness gate, coalesced delivery,
    commit-after-delivery, request consumption, one-shot firing,
    persist. Returns a report."""
    now = now or time.time()
    state = _load_state()
    report = {"fired": [], "errors": [], "requests": 0, "flips": []}
    _walk_timed_flips(state, deliver, do_flip, now, report)
    _fire_daily_flips(state, do_flip, now, report)
    for config in FrameworkConfig.from_env().list_cousins():
        try:
            slug = config.slug
            if config.type == "worker":
                _fire_worker_loops(config, state, now, report)
                continue
            if not is_alive(slug):
                continue
            home = FrameworkConfig.from_env().root / "cousins" / slug
            loops, errors = load_cousin_loops(home)
            report["errors"].extend(errors)
            # The beat first, then loops - one coalesced delivery.
            sections = []
            beat_commit = None
            interval = config.heartbeat_seconds
            last_beat = state["last_beat"].get(slug, 0)
            if interval > 0 and (now - last_beat) >= interval:
                beat_prompt, beat_commit = _compose_beat(home, now)
                if beat_prompt:
                    sections.append(("context-heartbeat", beat_prompt))
            due = []
            for loop in loops:
                key = "%s|%s" % (slug, loop["name"])
                if _loop_due(loop, state["last_fires"].get(key, 0),
                             now):
                    due.append(loop)
                    sections.append((loop["name"], loop["prompt"]))
            if not sections:
                continue
            if len(sections) == 1:
                text = sections[0][1]
            else:
                text = ("[Framework scheduler: %d loops due this tick"
                        " - handle in order]\n\n" % len(sections)
                        + "\n\n".join("### %s\n%s" % (name, prompt)
                                      for name, prompt in sections))
            # Commit-after-delivery: a failed injection leaves the
            # beat's delta unconsumed and every due loop still due.
            if deliver(slug, text):
                if beat_commit is not None:
                    beat_commit()
                    state["last_beat"][slug] = now
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


def _default_is_alive(slug):
    """Liveness = the cousin's chat server answers on its port; a
    lingering pane with a dead server must not receive fires."""
    import socket

    try:
        config = CousinConfig.load(
            FrameworkConfig.from_env().root / "cousins" / slug)
        with socket.create_connection(
                ("127.0.0.1", config.require_chat_port()),
                timeout=1.5):
            return True
    except Exception:
        return False


def _default_deliver(slug, text):
    from cousin_lib.server.injection import TmuxInjector

    config = CousinConfig.load(
        FrameworkConfig.from_env().root / "cousins" / slug)
    injector = TmuxInjector(config.tmux_session)
    injector.inject(text)
    return True


def loops_main(argv=None):
    """cousin-loops: run the daemon, or inspect its state. Exit codes:
    status returns 0 healthy / 1 down-or-never-run, everything else
    0 ok / 2 usage."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="cousin-loops")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--ticks", type=int, default=0,
                   help="run N ticks then exit (0 = forever)")
    sub.add_parser("status")
    sub.add_parser("requests")
    p = sub.add_parser("fire")
    p.add_argument("slug")
    p.add_argument("loop")
    args = parser.parse_args(argv)
    if args.cmd == "status":
        status = daemon_status()
        print(status["message"])
        return 0 if status["ok"] else 1
    if args.cmd == "requests":
        rows = list_requests()
        if not rows:
            print("(no requests)")
        for row in rows:
            print("#%d %-8s %-6s %s %s"
                  % (row["id"], row["status"], row["kind"],
                     row["cousin"], row["payload"]))
        return 0
    if args.cmd == "fire":
        request_id = submit_request(
            "fire", cousin=args.slug, payload={"loop": args.loop})
        print("request #%d pending; the daemon consumes it on its"
              " next tick" % request_id)
        return 0
    # run
    count = 0
    while True:
        report = tick(deliver=_default_deliver,
                      is_alive=_default_is_alive)
        for error in report["errors"]:
            print("cousin-loops: %s" % error, file=sys.stderr)
        count += 1
        if args.ticks and count >= args.ticks:
            return 0
        time.sleep(args.interval)
