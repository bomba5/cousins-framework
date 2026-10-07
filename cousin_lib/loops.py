"""The loops daemon: recurring work with one owner.

docs/reference/loops.md is the contract. The structural rule everything
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

from cousin_lib.config import (CousinConfig, FrameworkConfig,
                               MissingConfigError, default_flip_at,
                               flip_time, parse_flip_at)

REQUEST_TTL_SECONDS = 600
READY_SUFFIX = ".ready"
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


def cancel_request(request_id):
    """Mark a PENDING request cancelled; True when a row changed. A
    cancelled row is consumed by nobody and reported like any other
    terminal status - the console's flip/cancel and any CLI use this."""
    con = _db()
    try:
        cur = con.execute(
            "UPDATE requests SET status='cancelled', consumed_at=?,"
            " reason='cancelled by request' WHERE id=? AND status='pending'",
            (time.time(), request_id))
        con.commit()
        return cur.rowcount > 0
    finally:
        con.close()


def _state_path():
    return FrameworkConfig.from_env().root / "data" / "loops-state.json"


def _fires_path():
    return FrameworkConfig.from_env().root / "data" / "loops-fires.jsonl"


def _log_fire(slug, name, now):
    """One line per delivered fire, appended at the same point the tick
    commits last_fires. The console's drift view reads this series; a
    failed append is reported by the caller, never fatal to the tick."""
    path = _fires_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as fh:
        fh.write(json.dumps({"ts": now, "cousin": slug, "loop": name})
                 + "\n")


def _health(report, key, ok, error=None):
    """One result for the health record (cousin_lib.health): the daemon
    folds report["health"] into data/health.json after each tick."""
    report.setdefault("health", []).append((key, bool(ok), None if ok else str(error)))


def read_fires(*, limit=None):
    """The fire log as dicts, oldest first; unparsable lines skipped.
    limit keeps the newest N."""
    try:
        lines = _fires_path().read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "ts" in row:
            rows.append(row)
    if limit:
        rows = rows[-limit:]
    return rows


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


def load_cousin_loops(home, *, include_disabled=False):
    """([loops], [errors]). A malformed cousin.toml or invalid loop is
    a reported error NAMING its source - an earlier version returned
    an empty list on any parse error, which silently disabled every
    loop the cousin had. The daemon wants only enabled loops; a viewer
    (the console's loops editor) asks for the disabled ones too."""
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
        if forms[0] == "cron" and cron_error(entry["cron"]):
            errors.append("loop %r in %s: cron %r: %s"
                          % (name, home, entry["cron"], cron_error(entry["cron"])))
            continue
        if not entry.get("enabled", True) and not include_disabled:
            continue  # truthiness, by spec
        loops.append(entry)
    return loops, errors


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def validate_loops(entries):
    """Every reason a [[loops]] array would be refused, each naming its
    index: the name pattern and uniqueness, exactly one schedule form,
    a non-empty prompt, days a list of three-letter weekdays. Empty
    list means valid."""
    if not isinstance(entries, list):
        return ["loops must be a list"]
    errors, seen = [], set()
    for idx, entry in enumerate(entries):
        tag = "loops[%d]" % idx
        if not isinstance(entry, dict):
            errors.append("%s: not a table" % tag)
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            errors.append("%s: invalid name %r" % (tag, name))
        elif name in seen:
            errors.append("%s: duplicate name %r" % (tag, name))
        seen.add(name)
        forms = [f for f in _SCHEDULE_FORMS if f in entry]
        if len(forms) != 1:
            errors.append("%s: exactly one schedule form required, has %r"
                          % (tag, forms))
        elif forms[0] == "interval_seconds":
            iv = entry["interval_seconds"]
            if isinstance(iv, bool) or not isinstance(iv, int) or iv <= 0:
                errors.append("%s: interval_seconds must be a positive"
                              " integer" % tag)
        elif not isinstance(entry[forms[0]], str) or not entry[forms[0]]:
            errors.append("%s: %s must be a non-empty string"
                          % (tag, forms[0]))
        elif forms[0] == "cron" and cron_error(entry["cron"]):
            errors.append("%s: cron %r: %s" % (tag, entry["cron"],
                                               cron_error(entry["cron"])))
        prompt = entry.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            errors.append("%s: empty prompt" % tag)
        days = entry.get("days")
        if days is not None:
            if (not isinstance(days, list)
                    or any(not isinstance(d, str)
                           or d.lower()[:3] not in _WEEKDAYS
                           or len(d) != 3 for d in days)):
                errors.append("%s: days must be a list of three-letter"
                              " weekdays" % tag)
    return errors


def _toml_string(value):
    # A JSON string literal is a valid TOML basic string for every
    # character json.dumps emits with ensure_ascii off.
    return json.dumps(value, ensure_ascii=False)


def _render_loop(entry):
    lines = ["[[loops]]", "name = %s" % _toml_string(entry["name"])]
    for form in _SCHEDULE_FORMS:
        if form in entry:
            value = entry[form]
            lines.append("%s = %s" % (form, value if form == "interval_seconds"
                                      else _toml_string(value)))
    if entry.get("days") is not None:
        lines.append("days = [%s]" % ", ".join(
            _toml_string(d.lower()) for d in entry["days"]))
    lines.append("prompt = %s" % _toml_string(entry["prompt"]))
    lines.append("enabled = %s"
                 % ("true" if entry.get("enabled", True) else "false"))
    if entry.get("hidden"):
        lines.append("hidden = true")
    return "\n".join(lines) + "\n"


def _strip_loops_tables(text):
    """Remove every [[loops]] table from a cousin.toml text; the other
    tables keep their lines and order."""
    out, skipping = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[[loops]]"):
            skipping = True
            continue
        if skipping and stripped.startswith("["):
            skipping = False
        if not skipping:
            out.append(line)
    return "".join(out)


def _normalized(entries):
    norm = []
    for e in entries:
        item = {"name": e["name"], "prompt": e["prompt"],
                "enabled": bool(e.get("enabled", True))}
        for form in _SCHEDULE_FORMS:
            if form in e:
                item[form] = e[form]
        if e.get("days") is not None:
            item["days"] = [d.lower() for d in e["days"]]
        if e.get("hidden"):
            item["hidden"] = True
        norm.append(item)
    return norm


def save_cousin_loops(home, entries):
    """Replace the whole [[loops]] array of <home>/cousin.toml: validate
    (ValueError naming the index), render, re-parse the new text and
    check it round-trips, then rename into place. Returns the
    normalized entries as the file now holds them."""
    import os
    import tomllib
    errors = validate_loops(entries)
    if errors:
        raise ValueError("; ".join(errors))
    path = Path(home) / "cousin.toml"
    text = path.read_text()
    kept = _strip_loops_tables(text).rstrip("\n")
    wanted = _normalized(entries)
    rendered = "\n\n".join(_render_loop(e) for e in wanted)
    new_text = kept + "\n" + ("\n" + rendered if rendered else "")
    parsed = tomllib.loads(new_text)
    if _normalized(parsed.get("loops", [])) != wanted:
        raise ValueError("loops did not round-trip through TOML")
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(new_text)
    os.replace(tmp, path)
    return wanted


# (name, minimum, maximum) of the five cron fields, in order
_CRON_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day of month", 1, 31),
                ("month", 1, 12), ("day of week", 0, 7))


def _parse_cron_field(field, minimum, maximum):
    """The values one cron field names. ValueError for anything else: a
    word (names like `mon` are not understood), a value outside
    minimum..maximum, a step below 1, an empty part."""
    values = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step < 1:
                raise ValueError("step %d is below 1" % step)
        if part == "*":
            lo, hi = minimum, maximum
        elif "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
        else:
            lo = hi = int(part)
        if not minimum <= lo <= hi <= maximum:
            raise ValueError("%s is outside %d-%d" % (part, minimum, maximum))
        values.update(range(lo, hi + 1, step))
    return values


def cron_error(expr):
    """Why the five-field cron `expr` cannot be read, or None. The daemon
    matches with the same parser, so an expression this passes never
    raises in a tick."""
    if not isinstance(expr, str):
        return "not a string"
    fields = expr.split()
    if len(fields) != len(_CRON_FIELDS):
        return "%d fields, cron has 5" % len(fields)
    for text, (name, lo, hi) in zip(fields, _CRON_FIELDS):
        try:
            _parse_cron_field(text, lo, hi)
        except ValueError as err:
            return "%s %r: %s" % (name, text, err)
    return None


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


def next_due(loop, last_fire, *, now=None, horizon_days=8):
    """The next time the daemon's own due logic would fire this loop:
    interval loops from the last fire (now, when never fired); daily_at
    and cron by scanning forward minute by minute. 0 when nothing within
    the horizon."""
    now = now or time.time()
    if "interval_seconds" in loop:
        interval = int(loop.get("interval_seconds") or 0)
        if interval <= 0:
            return 0
        if not last_fire:
            return int(now)
        return int(max(now, last_fire + interval))
    when = datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    step = 60
    for minute in range(0, horizon_days * 24 * 60):
        candidate = when.timestamp() + minute * step
        if candidate < now - step:
            continue
        if "cron" in loop:
            try:
                if cron_matches(loop["cron"],
                                datetime.fromtimestamp(candidate)):
                    return int(candidate)
            except (ValueError, IndexError):
                return 0
        elif "daily_at" in loop:
            if _loop_due(loop, last_fire, candidate):
                return int(candidate)
    return 0


def _fire_worker_loops(config, state, now, report):
    """A worker cousin has no session and no beats; a due loop runs
    the worker command template from host configuration as a tracked
    background job. The EXIT CODE lands in the job row (the jobs
    module's detached runner writes it back), so a worker failing
    every firing looks failed everywhere loop state is shown - the
    source marked fires successful before the subprocess ran. With no
    worker-cmd configured the loop STAYS DUE and the error names the
    remediation. Returns the errors loading the cousin's loops."""
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
            _health(report, "loop:" + key, True)
            continue
        if not template:
            report["errors"].append(
                "worker loop %s due but no worker command configured;"
                " write config/worker-cmd (loop stays due)" % key)
            _health(report, "loop:" + key, False, report["errors"][-1])
            continue
        cmd = [part.replace("{prompt}", loop["prompt"])
                   .replace("{home}", str(home))
               for part in shlex.split(template)]
        job_id = jobs.register_job(
            kind="other", title="worker %s" % key,
            spawned_by=config.slug, command=" ".join(cmd))
        log_path = jobs._default_log_path(job_id)
        jobs.set_log_path(job_id, str(log_path))
        jobs.record_spawn(job_id, jobs._spawn_tracked(cmd, log_path, job_id))
        # The RUN is the firing; the rc arrives in the job row when
        # the detached runner finishes.
        state["last_fires"][key] = now
        report["fired"].append(key)
        _health(report, "loop:" + key, True)
        try:
            _log_fire(config.slug, loop["name"], now)
        except OSError as err:
            report["errors"].append("fire log: %s" % err)
    return errors


def _consume_requests(state, deliver, errors):
    """Fire requests (the console's fire button, `cousin-loops fire`,
    a ready file asking for the beat). Returns ["#id: reason"] for each
    request this call ended failed."""
    failed = []
    con = _db()
    try:
        # POSITIVE filter: this consumer claims only the kinds it
        # actually handles - "mine only if proven", never "mine unless
        # proven otherwise". A negative filter (the first fix here)
        # merely narrowed the kind-eating bug to every kind not yet
        # invented; with the positive form, an unknown kind is
        # invisible to every consumer until one is taught about it,
        # and its TTL expires it loudly.
        rows = con.execute(
            "SELECT * FROM requests WHERE status='pending'"
            " AND kind IN ('fire') ORDER BY id").fetchall()
        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            slug = row["cousin"]
            home = FrameworkConfig.from_env().root / "cousins" / slug
            loops, errs = load_cousin_loops(home)
            errors.extend(errs)
            commit = None
            if payload.get("loop") == "context-heartbeat":
                # The canonical beat by name: the daemon's own
                # composition, its delta committed after delivery like
                # the scheduled beat (the console's fire button and the
                # ready-file trigger both ask for it this way).
                text, commit = _compose_beat(home, time.time())
                if not text:
                    _finish_request(con, row["id"], "failed",
                                    "heartbeat composition failed")
                    failed.append("#%d: heartbeat composition failed"
                                  % row["id"])
                    continue
            else:
                target = next(
                    (l for l in loops
                     if l["name"] == payload.get("loop")), None)
                if target is None:
                    _finish_request(con, row["id"], "failed",
                                    "no such loop %r"
                                    % payload.get("loop"))
                    failed.append("#%d: no such loop %r"
                                  % (row["id"], payload.get("loop")))
                    continue
                text = ("[Framework scheduler: manual fire]\n\n"
                        "### %s\n%s" % (target["name"], target["prompt"]))
            ok = deliver(slug, text)
            if ok and commit is not None:
                commit()
                state["last_beat"][slug] = time.time()
            _finish_request(con, row["id"],
                            "done" if ok else "failed",
                            "" if ok else "delivery failed")
            if not ok:
                failed.append("#%d: delivery failed for %s"
                              % (row["id"], slug))
    finally:
        con.close()
    return failed


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


def _fire_daily_flips(state, do_flip, is_alive, now, report):
    """flip_at drivers: late-once per day, and AT MOST ONE flip per
    tick - the tick cadence is the stagger that keeps boot packets
    from assembling simultaneously. A stopped cousin is skipped and
    its day marked done: a flip starts the agent, so flipping a
    cousin the operator stopped would undo the stop.

    The point is that a session is at most a day old, so the flip ends
    a session that started before today's flip time (late when the
    daemon was down at it). A session that started at or after it
    (a cousin spawned or started since), or none at all, is younger
    than the flip point: its day is marked done, not flipped seconds
    after its first turn (boot.generation_started)."""
    from cousin_lib import boot
    if report["flips"]:
        return  # a timed flip already used this tick's slot
    when = datetime.fromtimestamp(now)
    framework = FrameworkConfig.from_env()
    for config in framework.list_cousins():
        try:
            at = flip_time(config, framework.root)
        except MissingConfigError as err:
            report["errors"].append(str(err))
            continue
        if not at:
            continue        # a worker, or an explicit flip_at = "never"
        hour, minute = parse_flip_at(at, "flip_at for %s" % config.slug)
        target = when.replace(hour=hour, minute=minute, second=0,
                              microsecond=0).timestamp()
        last = state.setdefault("last_flips", {}).get(config.slug)
        if now >= target and last != str(when.date()):
            started = boot.generation_started(config.home)
            if (started is None or started >= target
                    or not is_alive(config.slug)):
                state["last_flips"][config.slug] = str(when.date())
                continue
            result = do_flip(config.slug)
            state["last_flips"][config.slug] = str(when.date())
            report["flips"].append(config.slug)
            if not result.get("ok"):
                report["errors"].append(
                    "daily flip failed for %s: %s"
                    % (config.slug, result.get("error", "?")))
            return  # one per tick


def _remove_ready(path, report, why):
    try:
        path.unlink()
    except OSError as err:
        report["errors"].append("cannot remove %s: %s" % (path, err))
        return
    report["errors"].append("%s removed: %s" % (path, why))


def _fire_ready_files(slug, home, loops, state, deliver, now, report):
    """Trigger files: every <home>/<name>.ready is a request to fire
    now. A name matching a [[loops]] entry delivers that loop's prompt
    (an extra fire that leaves the schedule alone, like a manual
    fire); "context-heartbeat" delivers the daemon's own beat
    composition and commits its delta; a name ending in "-message"
    delivers the file's contents as a literal line. Anything else is
    removed with a report line and never delivered.

    The file is the dedup state, and it commits after delivery like
    everything else: a failed delivery leaves the file where it is,
    reported once (not once per tick), and the next tick tries again.
    An earlier version ran this as a separate watcher process with
    its own tmux path and its own seen-set; here it is a tick step, so
    there is exactly one owner of delivery and one liveness gate."""
    home = Path(home)
    try:
        entries = sorted(p for p in home.iterdir()
                         if p.is_file() and p.name.endswith(READY_SUFFIX))
    except OSError as err:
        report["errors"].append(
            "%s: cannot list ready files: %s" % (slug, err))
        return
    reported = state.setdefault("ready_reported", {})
    by_name = {loop["name"]: loop for loop in loops}
    for path in entries:
        name = path.name[:-len(READY_SUFFIX)]
        key = "%s|%s" % (slug, name)
        commit = None
        if name in by_name:
            text = ("[Framework scheduler: ready-file trigger]\n\n"
                    "### %s\n%s" % (name, by_name[name]["prompt"]))
        elif name == "context-heartbeat":
            text, commit = _compose_beat(home, now)
            if not text:
                report["errors"].append(
                    "%s: heartbeat composition failed; file stays"
                    % path)
                continue
        elif name.endswith("-message"):
            try:
                text = path.read_text(errors="replace").strip()
            except OSError as err:
                report["errors"].append(
                    "%s: unreadable, file stays: %s" % (path, err))
                continue
            if not text:
                _remove_ready(path, report, "empty message")
                continue
        else:
            _remove_ready(path, report,
                          "unknown trigger name %r for %s" % (name, slug))
            continue
        if deliver(slug, text):
            _health(report, "delivery:" + slug, True)
            if commit is not None:
                commit()
                state["last_beat"][slug] = now
            try:
                path.unlink()
            except OSError as err:
                report["errors"].append(
                    "%s delivered but cannot remove the file: %s"
                    % (path, err))
            reported.pop(key, None)
            report["ready"].append(key)
        else:
            _health(report, "delivery:" + slug, False,
                    "delivery failed for %s; the file stays" % path)
            if key not in reported:
                reported[key] = now
                report["errors"].append(
                    "delivery failed for %s; the file stays" % path)


class _CousinNotAlive(Exception):
    """A one-shot whose cousin is down: held pending, not an error."""


def _fire_one_shots(deliver, is_alive, now, report):
    """Tick step 4: fire due one-shots from the scheduler store
    (cousin_lib.schedule) through the daemon's own delivery seam.
    schedule.tick owns the store semantics - mark fired only after
    delivery returned, per-job isolation - and this adapter only maps
    the daemon's seam onto it: the provenance prefix, the liveness
    gate (a down cousin keeps its job pending until it returns), and a
    False delivery counted as the failure it is."""
    from cousin_lib import schedule

    def deliver_one(slug, prompt):
        if not is_alive(slug):
            raise _CousinNotAlive(slug)
        if not deliver(slug, "[cousin-schedule] %s" % prompt):
            raise RuntimeError("delivery failed for %s" % slug)

    def on_error(job_id, err):
        if isinstance(err, _CousinNotAlive):
            return
        report["errors"].append(
            "scheduled job #%d kept pending: %s" % (job_id, err))

    report["scheduled"] = schedule.tick(
        now_ts=int(now), deliver=deliver_one, on_error=on_error)


# Every cousin's memory index is kept level with its sources by the
# daemon, by default: a cousin that never searches still has a fresh
# index, and a search never pays for a big catch-up. A home is checked
# at most this often; the work runs on one background thread, one home
# at a time, so the embedding service is never hit by several homes at
# once and the tick never waits on it.
INDEX_REFRESH_SECONDS = 300

_index_worker = {"thread": None, "queue": [], "last": {}, "done": []}


def _refresh_one(home):
    from cousin_lib import memory_search
    return memory_search.refresh_if_stale(home)


def schedule_index_refresh(now, report, *, homes, refresh=_refresh_one,
                           every=INDEX_REFRESH_SECONDS, background=True):
    """Queue each home not checked in `every` seconds and make sure one
    worker drains the queue. Results of finished passes land in
    report["indexed"] on the next tick."""
    import threading

    state = _index_worker
    report.setdefault("indexed", [])
    while state["done"]:
        report["indexed"].append(state["done"].pop(0))
    for slug, home in homes:
        if now - state["last"].get(slug, 0) < every:
            continue
        state["last"][slug] = now
        if (slug, home) not in state["queue"]:
            state["queue"].append((slug, home))

    def drain():
        while state["queue"]:
            slug, home = state["queue"].pop(0)
            try:
                out = refresh(home)
            except Exception as err:  # noqa: BLE001 - one home never stops the rest
                out = {"error": str(err)}
            if out:
                state["done"].append((slug, out))

    if not background:
        drain()
        while state["done"]:
            report["indexed"].append(state["done"].pop(0))
        return
    thread = state["thread"]
    if state["queue"] and (thread is None or not thread.is_alive()):
        state["thread"] = threading.Thread(
            target=drain, name="index-refresh", daemon=True)
        state["thread"].start()


# Dreaming (dreaming.py): a cousin whose [agent] dreaming is on gets a pass
# when one is due. One worker runs one pass at a time, each in a child
# process of its own (a pass scrubs auth variables, process-wide), so the
# tick never waits on a model.
_dream_worker = {"thread": None, "queue": [], "inflight": set(), "done": []}


def _dream_one(home, root, trigger):
    from cousin_lib import dreaming
    return dreaming.run_child(home, root, trigger)


def schedule_dreams(now, report, *, homes, root, run=_dream_one, background=True):
    """Queue each home a pass is due for (dreaming.due) and make sure one
    worker drains the queue. Finished passes land in report["dreamed"] on
    the next tick, as (slug, verdict)."""
    import threading
    from datetime import datetime
    from cousin_lib import dreaming

    state = _dream_worker
    report.setdefault("dreamed", [])
    while state["done"]:
        report["dreamed"].append(state["done"].pop(0))
    when = datetime.fromtimestamp(now).astimezone()
    for slug, home in homes:
        if slug in state["inflight"]:
            continue
        try:
            trigger = dreaming.due(home, when)
        except Exception as err:  # noqa: BLE001 - one home never stops the rest
            report["errors"].append("dreaming %s: %s" % (slug, err))
            _health(report, "dream-due:" + slug, False, err)
            continue
        _health(report, "dream-due:" + slug, True)
        if trigger:
            state["inflight"].add(slug)
            state["queue"].append((slug, home, trigger))

    def drain():
        while state["queue"]:
            slug, home, trigger = state["queue"].pop(0)
            try:
                out = run(home, root, trigger)
            except Exception as err:  # noqa: BLE001 - one pass never stops the rest
                out = {"result": "error", "error": str(err)}
            finally:
                state["inflight"].discard(slug)
            state["done"].append((slug, out))

    if not background:
        drain()
        while state["done"]:
            report["dreamed"].append(state["done"].pop(0))
        return
    thread = state["thread"]
    if state["queue"] and (thread is None or not thread.is_alive()):
        state["thread"] = threading.Thread(target=drain, name="dreaming", daemon=True)
        state["thread"].start()


DREAM_OK = ("done", "no_change", "budget")


def dream_outcome(out):
    """(ok, error) for a finished pass's verdict: done, no_change and
    budget (a result, not a fault) are ok; error, lost, or anything
    else is a failure, its error the pass's own."""
    out = out if isinstance(out, dict) else {}
    result = out.get("result")
    if result in DREAM_OK:
        return True, None
    return False, out.get("error") or "pass ended %s" % (result or "without a verdict")


def _default_do_flip(slug, reason="flip"):
    from cousin_lib.flip import flip
    return flip(slug, reason=reason)


def daily_flip(do_flip):
    """The daily cadence's flip: the default one says `max_age` (the model
    reads it in its handoff request); an injected one is used as given."""
    if do_flip is _default_do_flip:
        return lambda slug: _default_do_flip(slug, reason="max_age")
    return do_flip


def timed_flip(do_flip):
    if do_flip is _default_do_flip:
        return lambda slug: _default_do_flip(slug, reason="timed flip")
    return do_flip


def _keep_distilled(slug, home, report):
    """Keep the distilled floor level with raw: distill when raw was
    written after the last run. A stat per raw file per tick; the
    distill itself is well under a second. Without it the floor only
    moved at a start or a flip, and the console's memory view said
    "raw has entries newer than the distilled views" nearly always."""
    try:
        from cousin_lib import distill
        if distill.distill_if_behind(home):
            report["distilled"].append(slug)
    except Exception as err:  # noqa: BLE001 - never costs the tick
        report["errors"].append("distill failed for %s: %s" % (slug, err))
        _health(report, "distill:" + slug, False, err)
        return
    _health(report, "distill:" + slug, True)


def tick(*, deliver, is_alive, now=None, do_flip=_default_do_flip,
         index_refresh=False, dreams=False):
    """One scheduler tick, per docs/reference/loops.md: per-cousin
    exception isolation, liveness gate, coalesced delivery,
    commit-after-delivery, request consumption, one-shot firing
    (_fire_one_shots), persist. Returns a report.

    report["health"] is [(key, ok, error)], one result per component
    the tick ran (cousin_lib.health folds it into data/health.json):
    `cousin:<slug>` the cousin's walk (an exception, or loops that do
    not load), `loop:<slug>|<name>` a loop's due check (a worker's: its
    firing), `delivery:<slug>` a delivery to the cousin, `distill:<slug>`,
    `requests`, `schedules`, `index-refresh` and `index:<slug>`,
    `dream-due:<slug>` and `dreaming:<slug>` (a finished pass: error or
    lost fails, done, no_change and budget are ok), `outbox`, `meetings`."""
    now = now or time.time()
    state = _load_state()
    report = {"fired": [], "errors": [], "requests": 0, "flips": [],
              "ready": [], "scheduled": 0, "distilled": [], "health": []}
    # Expire first: the first tick after downtime must not fire a timed
    # flip or a manual fire that outlived its TTL (it never fires late).
    expire_stale_requests(now=now)
    _walk_timed_flips(state, deliver, timed_flip(do_flip), now, report)
    _fire_daily_flips(state, daily_flip(do_flip), is_alive, now, report)
    for config in FrameworkConfig.from_env().list_cousins():
        walk_errors = []
        try:
            slug = config.slug
            if config.type == "worker":
                walk_errors += _fire_worker_loops(config, state, now, report)
                continue
            home = FrameworkConfig.from_env().root / "cousins" / slug
            _keep_distilled(slug, home, report)
            if not is_alive(slug):
                continue
            loops, errors = load_cousin_loops(home)
            report["errors"].extend(errors)
            walk_errors += errors
            # Trigger files first: each is its own delivery, so its
            # removal maps one-to-one onto a delivery that succeeded.
            _fire_ready_files(slug, home, loops, state, deliver, now,
                              report)
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
                try:
                    is_due = _loop_due(loop, state["last_fires"].get(key, 0), now)
                except Exception as err:  # noqa: BLE001 - one loop never stops the rest
                    report["errors"].append("loop %s: %s" % (key, err))
                    _health(report, "loop:" + key, False, err)
                    continue
                _health(report, "loop:" + key, True)
                if is_due:
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
                _health(report, "delivery:" + slug, True)
                if beat_commit is not None:
                    beat_commit()
                    state["last_beat"][slug] = now
                for loop in due:
                    key = "%s|%s" % (slug, loop["name"])
                    state["last_fires"][key] = now
                    report["fired"].append(key)
                    try:
                        _log_fire(slug, loop["name"], now)
                    except OSError as err:
                        report["errors"].append("fire log: %s" % err)
            else:
                report["errors"].append(
                    "delivery failed for %s; loops stay due" % slug)
                _health(report, "delivery:" + slug, False,
                        report["errors"][-1])
        except Exception as err:
            # Per-cousin isolation: one flaky cousin never starves
            # the rest of the walk.
            report["errors"].append("%s: %s" % (config.slug, err))
            walk_errors.append("%s: %s" % (type(err).__name__, err))
        finally:
            _health(report, "cousin:" + config.slug, not walk_errors,
                    walk_errors[0] if walk_errors else None)
    failed = _consume_requests(state, deliver, report["errors"])
    _health(report, "requests", not failed, "; ".join(failed or ()))
    try:
        _fire_one_shots(deliver, is_alive, now, report)
    except Exception as err:
        # A broken scheduler store never costs the loops their tick.
        report["errors"].append("one-shots: %s" % err)
        _health(report, "schedules", False, err)
    else:
        kept = [e for e in report["errors"] if e.startswith("scheduled job #")]
        _health(report, "schedules", not kept, kept[0] if kept else None)
    if index_refresh:
        try:
            root = FrameworkConfig.from_env().root
            schedule_index_refresh(now, report, homes=[
                (c.slug, root / "cousins" / c.slug)
                for c in FrameworkConfig.from_env().list_cousins()
                if c.type != "worker"])
        except Exception as err:
            report["errors"].append("index refresh: %s" % err)
            _health(report, "index-refresh", False, err)
        else:
            _health(report, "index-refresh", True)
        for slug, out in report.get("indexed", []):
            error = out.get("error") if isinstance(out, dict) else None
            _health(report, "index:" + slug, not error, error)
    if dreams:
        try:
            root = FrameworkConfig.from_env().root
            schedule_dreams(now, report, root=root, homes=[
                (c.slug, root / "cousins" / c.slug)
                for c in FrameworkConfig.from_env().list_cousins()
                if c.type != "worker"])
        except Exception as err:
            report["errors"].append("dreaming: %s" % err)
            _health(report, "dreaming", False, err)
        else:
            _health(report, "dreaming", True)
        for slug, out in report.get("dreamed", []):
            _health(report, "dreaming:" + slug, *dream_outcome(out))
    try:
        from cousin_lib import outbox
        # its own clock, never the tick's start: the outbox's deadline and
        # pass budget have to see the time a slow send actually took
        report["outbox"] = outbox.drain(FrameworkConfig.from_env().root)
    except Exception as err:
        # A broken outbox never costs the loops their tick.
        report["errors"].append("outbox: %s" % err)
        _health(report, "outbox", False, err)
    else:
        _health(report, "outbox", True)
    try:
        from cousin_lib import meetings
        report["meetings"] = meetings.tick(deliver=deliver, now=now)
    except Exception as err:
        # A broken meetings store never costs the loops their tick.
        report["errors"].append("meetings: %s" % err)
        _health(report, "meetings", False, err)
    else:
        _health(report, "meetings", True)
    state["last_tick"] = now
    _save_state(state)
    return report


def _default_is_alive(slug):
    """Liveness for a tmux cousin = its chat server answers on its port
    (a lingering pane with a dead server must not receive fires); for a
    runner cousin, delivery.is_alive reads the runner's lock instead and
    never opens the port."""
    import socket

    from cousin_lib import delivery

    def _chat_port_open():
        try:
            config = CousinConfig.load(
                FrameworkConfig.from_env().root / "cousins" / slug)
            with socket.create_connection(
                    ("127.0.0.1", config.require_chat_port()),
                    timeout=1.5):
                return True
        except Exception:
            return False

    home = FrameworkConfig.from_env().root / "cousins" / slug
    return delivery.is_alive(home, fallback=_chat_port_open)


def _default_deliver(slug, text):
    from cousin_lib import delivery

    home = FrameworkConfig.from_env().root / "cousins" / slug
    item = delivery.Item(thread_id=delivery.thread_id("loop", "daemon"),
                         source="loop", body=text)
    # Anything the deliverer does not accept (skipped at a menu, failed,
    # or a queued row for a tmux cousin) is not a delivery: the caller
    # keeps the beat or prompt due. A runner cousin is not waited on: the
    # durable inbox put is the acceptance, and a tick must not block on
    # a turn.
    wait = not isinstance(delivery.backend_for(home), delivery.InboxBackend)
    return delivery.accepted(delivery.deliver(home, item, wait=wait), home)


LOCK_HELD_EXIT = 5    # `run`: another loops daemon holds <root>/run/loops.lock (busy)


class LoopsLockHeld(Exception):
    """Another loops daemon runs on this root: `run` exits LOCK_HELD_EXIT,
    busy, not 2 (configuration), so a supervisor waits for the holder."""


def _fd_path(fd):
    """The path an open fd names (/proc/self/fd), or None where there is
    no /proc to ask."""
    import os
    try:
        return os.readlink("/proc/self/fd/%d" % fd)
    except OSError:
        return None


def hold_loops_lock(root):
    """One clock per root: an exclusive, non-blocking flock on
    <root>/run/loops.lock, held until the returned descriptor is closed
    (the kernel drops it when the process dies, even on SIGKILL). Two
    daemons on one root would each fire every due one-shot, heartbeat,
    [[loops]] entry and daily flip: nothing else claims them. Raises
    LoopsLockHeld when another process holds it.

    flock locks are shared across fork: a worker loop's job runs through
    jobs._spawn_tracked, which forks twice (no exec) from inside this
    process, and the job-runner child would otherwise inherit our copy
    of the fd and keep the lock held after we exit - no clock, until the
    job ends. os.register_at_fork closes our copy in every child forked
    from here on. Once registered, a handler is never unregistered, so a
    process that calls this more than once (every test in one interpreter,
    a daemon that re-acquires after losing the lock) accumulates one
    handler per call; the fd number a stale handler names can be reused
    for something unrelated by the time a later, unrelated fork runs it,
    and closing that would be a bug of its own, not a fix. Each handler
    therefore checks the fd is still open on the same file (device and
    inode) it locked before closing it, and, where /proc/self/fd says,
    at the same path: a deleted lock file's inode can be recycled for a
    new file (a later temporary root, in a test run) that a reused fd
    number then names, and device and inode alone would close it."""
    import fcntl
    import os
    path = Path(root) / "run" / "loops.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise LoopsLockHeld("another loops daemon holds %s" % path)
    identity = os.fstat(fd)
    ident_key = (identity.st_dev, identity.st_ino)
    ident_path = _fd_path(fd)

    def _close_in_child():
        try:
            st = os.fstat(fd)
        except OSError:
            return    # already closed: nothing to do
        if (st.st_dev, st.st_ino) != ident_key:
            return    # the fd number was reused for something else; not ours
        if ident_path is not None and _fd_path(fd) != ident_path:
            return    # a recycled inode under another path: not ours either
        try:
            os.close(fd)
        except OSError:
            pass

    os.register_at_fork(after_in_child=_close_in_child)
    return fd


def _record_health(results):
    """Fold one tick's results into data/health.json; a health write
    that fails is a line on stderr, never the tick's failure."""
    import sys
    try:
        from cousin_lib import health
        health.record(FrameworkConfig.from_env().root, results)
    except Exception as err:  # noqa: BLE001 - the record never costs a tick
        print("cousin-loops: health record: %s" % err, file=sys.stderr)


def loops_main(argv=None):
    """cousin-loops: run the daemon, or inspect its state. Exit codes:
    status returns 0 healthy / 1 down-or-never-run, everything else
    0 ok / 2 usage, and `run` exits LOCK_HELD_EXIT (5, busy) when another
    loops daemon holds the root's lock."""
    import argparse
    import os
    import sys

    parser = argparse.ArgumentParser(prog="cousin-loops")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--ticks", type=int, default=0,
                   help="run N ticks then exit (0 = forever)")
    sub.add_parser("status")
    sub.add_parser("requests")
    sub.add_parser("flips", help="each cousin's daily flip time and"
                                 " where it comes from")
    p = sub.add_parser("fire")
    p.add_argument("slug")
    p.add_argument("loop")
    args = parser.parse_args(argv)
    try:
        FrameworkConfig.for_command()
    except MissingConfigError as err:
        print("cousin-loops: %s" % err, file=sys.stderr)
        return 2
    if args.cmd == "status":
        status = daemon_status()
        print(status["message"])
        return 0 if status["ok"] else 1
    if args.cmd == "flips":
        framework = FrameworkConfig.from_env()
        default = default_flip_at(framework.root)
        print("install default: %s" % (default or "never"))
        for config in framework.list_cousins():
            try:
                at = flip_time(config, framework.root)
            except MissingConfigError as err:
                print("  %-12s ERROR  %s" % (config.slug, err))
                continue
            if config.type == "worker":
                source = "worker, never flips"
            elif config.flip_at is not None:
                source = "its own cousin.toml"
            else:
                source = "install default"
            print("  %-12s %-6s %s"
                  % (config.slug, at or "never", source))
        return 0
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
    # run: the one clock of this root, for as long as it runs
    root = FrameworkConfig.from_env().root
    try:
        lock_fd = hold_loops_lock(root)
    except LoopsLockHeld as err:
        print("cousin-loops: %s" % err, file=sys.stderr)
        return LOCK_HELD_EXIT
    from cousin_lib import version
    version.announce(root, "loops")         # cousin-upgrade's restart check
    try:
        count = 0
        while True:
            try:
                report = tick(deliver=_default_deliver,
                              is_alive=_default_is_alive, index_refresh=True,
                              dreams=True)
            except Exception as err:
                # The tick's own failure is the one the daemon dies of:
                # on record before the supervisor restarts it.
                _record_health([("tick", False, "%s: %s"
                                 % (type(err).__name__, err))])
                raise
            _record_health(report["health"] + [("tick", True, None)])
            for slug, out in report.get("indexed", []):
                print("cousin-loops: index %s: %s" % (slug, out),
                      file=sys.stderr)
            for slug, out in report.get("dreamed", []):
                print("cousin-loops: dreaming %s: %s" % (slug, json.dumps(out)),
                      file=sys.stderr)
            for error in report["errors"]:
                print("cousin-loops: %s" % error, file=sys.stderr)
            count += 1
            if args.ticks and count >= args.ticks:
                return 0
            time.sleep(args.interval)
    finally:
        os.close(lock_fd)


if __name__ == "__main__":  # `python -m cousin_lib.loops`: how cousin-supervisor starts it
    import sys
    sys.exit(loops_main())
