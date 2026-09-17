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

from cousin_lib.config import (CousinConfig, FrameworkConfig,
                               MissingConfigError, harness_config)

REQUEST_TTL_SECONDS = 600
READY_SUFFIX = ".ready"
GUARD_FLIP_DELAY_SECONDS = 300
_MB = 1024 * 1024
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
    a reported error NAMING its source - the source framework returned
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
        try:
            _log_fire(config.slug, loop["name"], now)
        except OSError as err:
            report["errors"].append("fire log: %s" % err)


def _consume_requests(state, deliver, errors):
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
                    continue
            else:
                target = next(
                    (l for l in loops
                     if l["name"] == payload.get("loop")), None)
                if target is None:
                    _finish_request(con, row["id"], "failed",
                                    "no such loop %r"
                                    % payload.get("loop"))
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
    The source framework ran this as a separate watcher process with
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
        elif key not in reported:
            reported[key] = now
            report["errors"].append(
                "delivery failed for %s; the file stays" % path)


def _pending_flip_cousins():
    con = _db()
    try:
        rows = con.execute(
            "SELECT DISTINCT cousin FROM requests"
            " WHERE status='pending' AND kind='flip'").fetchall()
        return {row["cousin"] for row in rows}
    finally:
        con.close()


def _guard_transcript_size(is_alive, now, report):
    """The transcript-size guard: when config/harness.toml sets
    flip_when_transcript_mb and a live cousin's session transcript
    (<transcripts_dir>/<runtime.session_id>.jsonl) has grown past it,
    submit ONE timed-flip request through the request store - the
    same row an operator's timed flip is, with the same warning
    ladder - for the largest offender only, at most one cousin per
    tick, and never a second while one is pending for that cousin.
    Workers, dead cousins, cousins without a persisted session id and
    absent transcripts are skipped without comment; a threshold with
    no transcripts_dir to measure against is a dead key and is said."""
    root = FrameworkConfig.from_env().root
    try:
        cfg = harness_config(root)
    except MissingConfigError as err:
        report["errors"].append("size guard off: %s" % err)
        return
    if not cfg or cfg.get("flip_when_transcript_mb") is None:
        return
    threshold_mb = cfg["flip_when_transcript_mb"]
    if not cfg.get("transcripts_dir"):
        report["errors"].append(
            "config/harness.toml sets flip_when_transcript_mb but not"
            " transcripts_dir; the size guard has nothing to measure")
        return
    from cousin_lib.flip import _read_session_id
    from cousin_lib.transcript_mine import transcript_path

    over = []
    for config in FrameworkConfig.from_env().list_cousins():
        if config.type == "worker" or not is_alive(config.slug):
            continue
        session_id = _read_session_id(config.home)
        if not session_id:
            continue
        path = transcript_path(config.home, root, session_id)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > threshold_mb * _MB:
            over.append((size, config.slug))
    pending = _pending_flip_cousins() if over else set()
    for size, slug in sorted(over, reverse=True):
        if slug in pending:
            continue
        submit_request("flip", cousin=slug, payload={
            "fire_at": now + GUARD_FLIP_DELAY_SECONDS,
            "reason": "transcript over %s MB (%.1f MB)"
                      % (threshold_mb, size / _MB)})
        report["guarded"].append(slug)
        return  # one cousin per tick


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
    report = {"fired": [], "errors": [], "requests": 0, "flips": [],
              "ready": [], "guarded": []}
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
                    try:
                        _log_fire(slug, loop["name"], now)
                    except OSError as err:
                        report["errors"].append("fire log: %s" % err)
            else:
                report["errors"].append(
                    "delivery failed for %s; loops stay due" % slug)
        except Exception as err:
            # Per-cousin isolation: one flaky cousin never starves
            # the rest of the walk.
            report["errors"].append("%s: %s" % (config.slug, err))
    _consume_requests(state, deliver, report["errors"])
    try:
        _guard_transcript_size(is_alive, now, report)
    except Exception as err:
        # The guard is advisory; it never costs the tick.
        report["errors"].append("size guard: %s" % err)
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
