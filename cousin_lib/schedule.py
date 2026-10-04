"""One-shot prompt scheduler.

`cousin-schedule add` stores a prompt with a target time; the loops
daemon's tick (cousin_lib.loops, step 4 of docs/reference/loops.md) fires
everything due by handing the prompt to its delivery seam, and
`cousin-schedule tick` does the same by hand when no daemon runs. Jobs are scoped per cousin; the database
is shared per install so one tick serves the whole fleet.

Identity and root come from the environment (COUSIN_HOME,
FRAMEWORK_ROOT) and fail loud when absent - a scheduler with a
defaulted cousin fires someone else's prompts.
"""
import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.trace import traced_cli
from cousin_lib.sqlite_util import wal


def _db_path():
    return FrameworkConfig.from_env().root / "data" / "scheduled.db"


def _db():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    wal(conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS scheduled_jobs ("
        " id         INTEGER PRIMARY KEY AUTOINCREMENT,"
        " cousin     TEXT NOT NULL,"
        " target_ts  INTEGER NOT NULL,"
        " prompt     TEXT NOT NULL,"
        " status     TEXT NOT NULL DEFAULT 'pending',"
        " created_at INTEGER NOT NULL,"
        " fired_at   INTEGER)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sj_pending"
        " ON scheduled_jobs(status, target_ts)"
    )
    conn.commit()
    return conn


def parse_when(when, *, now=None):
    """'in 30m' / 'tomorrow 06:30' / ISO datetime -> unix timestamp.
    The bare-number form defaults to minutes."""
    when = when.strip()
    now = now or datetime.now().astimezone()
    m = re.match(r"^in\s+(\d+)\s*([smhd]?)$", when, re.I)
    if m:
        n = int(m.group(1))
        unit = (m.group(2) or "m").lower()
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
                 "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return int((now + delta).timestamp())
    m = re.match(r"^tomorrow\s+(\d{1,2}):(\d{2})$", when, re.I)
    if m:
        target = (now + timedelta(days=1)).replace(
            hour=int(m.group(1)), minute=int(m.group(2)),
            second=0, microsecond=0,
        )
        return int(target.timestamp())
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(when, fmt).astimezone().timestamp())
        except ValueError:
            continue
    raise ValueError(
        "could not parse %r; try 'in 30m', 'tomorrow 18:00', or"
        " 'YYYY-MM-DDTHH:MM'" % when
    )


# A cousin may hold this many pending one-shots; `add` refuses the next.
# A runaway self-scheduling loop stops here instead of filling the store.
MAX_PENDING = 20

# A fire this many seconds after its target is reported as late; the
# daemon ticks every 30 s, so anything under two minutes is on time.
ON_TIME_S = 120

# Past this, the header also says the reason it was set may be settled.
STALE_S = 30 * 60


def _clock(ts):
    return datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d %H:%M %Z")


def annotate(job_id, prompt, *, created_ts, target_ts, now_ts):
    """The text a due job is delivered as: one header line, a blank
    line, then the prompt verbatim. The header tells the turn what it is
    (its own scheduled prompt, not a person), when it was set, when it
    was due and how late it fired. A late job is still delivered: the
    contract is at-least-once (see `tick`), so lateness is said, never
    turned into a silent drop."""
    late = now_ts - target_ts
    when = "on time" if late < ON_TIME_S else "%d min late" % (late // 60)
    header = ("#%d, set %s, due %s, fired %s (%s). Your own scheduled prompt:"
              " nobody is waiting on this turn unless the prompt says so."
              % (job_id, _clock(created_ts), _clock(target_ts), _clock(now_ts), when))
    if late >= STALE_S:
        header += (" It fired late (the cousin or the daemon was down), so what it"
                   " was set for may already be settled: check before acting on it.")
    return header + "\n\n" + prompt


def _print_error(job_id, err):
    print("cousin-schedule: job #%d delivery failed,"
          " kept pending: %s" % (job_id, err), file=sys.stderr)


def tick(*, now_ts=None, deliver, on_error=_print_error):
    """Fire every pending job whose time has come and return how many
    fired.

    Delivery is AT-LEAST-ONCE by contract, not by accident: a job is
    marked fired only AFTER its delivery returned, so a failed delivery
    stays pending and retries next tick, and a crash between delivery
    and the mark refires the job. A duplicate reminder is the accepted
    cost; losing one silently is not. Do not "fix" a duplicate by
    marking before delivering - that flips the contract to
    at-most-once, which loses reminders instead of repeating them.

    Delivered lines carry a provenance prefix and that is also
    contract, not cosmetics: a scheduled prompt is machine-authored
    text arriving on the same channel as human instruction, and
    unlabelled it is indistinguishable from the operator having said
    it. The scheduled text is preserved verbatim AFTER the prefix;
    callers must never assume the delivered line equals the scheduled
    string byte-for-byte.

    `deliver(cousin, prompt)` receives the prompt as `annotate` builds
    it (a header line with the job's times, then the scheduled text);
    adding the provenance prefix is the deliverer's job (`_default_deliver` here,
    the loops daemon's adapter there). Any exception it raises keeps
    that job pending and is reported through `on_error(job_id, err)`;
    so does an explicit `False` return (`_default_deliver` returns the
    producer's acceptance), reported as RuntimeError("delivery not
    accepted"). A truthy or `None` return marks the job fired. One
    failing job never stops the rest of the walk. For a runner
    cousin the at-least-once contract ends at the durable inbox put: a
    turn that fails after it is the runner's to handle, not a reason to
    fire the job again."""
    now_ts = now_ts or int(datetime.now().timestamp())
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, cousin, prompt, created_at, target_ts FROM scheduled_jobs"
            " WHERE status='pending' AND target_ts<=? ORDER BY target_ts",
            (now_ts,),
        ).fetchall()
        fired = 0
        for job_id, cousin, prompt, created_ts, target_ts in rows:
            text = annotate(job_id, prompt, created_ts=created_ts,
                            target_ts=target_ts, now_ts=now_ts)
            try:
                if deliver(cousin, text) is False:
                    raise RuntimeError("delivery not accepted")
            except Exception as err:
                on_error(job_id, err)
                continue
            conn.execute(
                "UPDATE scheduled_jobs SET status='fired', fired_at=?"
                " WHERE id=?",
                (now_ts, job_id),
            )
            conn.commit()
            fired += 1
        return fired
    finally:
        conn.close()


def _slug():
    return CousinConfig.from_env().slug


# ------------------------------------------------ library (the one implementation)

def add(slug, when, prompt, *, now=None):
    """Schedule one prompt for a cousin. ValueError for a bad or past
    time or an empty prompt. Returns the row as a dict."""
    now_dt = now or datetime.now()
    ts = parse_when(when, now=now_dt)           # raises ValueError itself
    if ts <= now_dt.timestamp():
        raise ValueError("target %s is in the past"
                         % datetime.fromtimestamp(ts).isoformat())
    if not (prompt or "").strip():
        raise ValueError("empty prompt")
    conn = _db()
    try:
        (pending,) = conn.execute(
            "SELECT count(*) FROM scheduled_jobs WHERE cousin=? AND status='pending'",
            (slug,)).fetchone()
        if pending >= MAX_PENDING:
            raise ValueError("%s already has %d pending scheduled prompts (the cap);"
                             " cancel one first" % (slug, pending))
        cur = conn.execute(
            "INSERT INTO scheduled_jobs (cousin, target_ts, prompt, created_at)"
            " VALUES (?, ?, ?, ?)", (slug, ts, prompt, int(now_dt.timestamp())))
        conn.commit()
        return {"id": cur.lastrowid, "cousin": slug, "target_ts": ts, "prompt": prompt}
    finally:
        conn.close()


def list_entries(slug=None, *, include_fired=False, limit=None):
    """The old CLI's two queries, unchanged: `include_fired` (the old
    `--all`) drops the status filter, orders newest-first and takes
    `limit` (the old CLI passed 50); pending-only keeps the status
    filter, orders oldest-first, and is never limited - exactly what
    `_cmd_list` did before this became a library call."""
    conn = _db()
    try:
        where, params = [], []
        if slug:
            where.append("cousin=?"); params.append(slug)
        if include_fired:
            sql = "SELECT id, cousin, target_ts, prompt, status FROM scheduled_jobs"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY target_ts DESC"
            if limit:
                sql += " LIMIT ?"; params.append(limit)
        else:
            where.append("status='pending'")
            sql = ("SELECT id, cousin, target_ts, prompt, status FROM scheduled_jobs"
                   " WHERE " + " AND ".join(where) + " ORDER BY target_ts")
        return [dict(zip(("id", "cousin", "target_ts", "prompt", "status"), r))
                for r in conn.execute(sql, params)]
    finally:
        conn.close()


def cancel(job_id, *, slug=None):
    conn = _db()
    try:
        sql = "UPDATE scheduled_jobs SET status='cancelled' WHERE id=? AND status='pending'"
        params = [job_id]
        if slug:
            sql += " AND cousin=?"; params.append(slug)
        cur = conn.execute(sql, params); conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def format_entries(entries):
    """Exactly the old CLI's per-row line: same field order, same
    padding, same 60-char prompt truncation. A single `print()` of the
    joined result reproduces the old per-row `print()` loop
    byte-for-byte."""
    if not entries:
        # _cmd_list never reaches this: its own "no jobs for..." message
        # runs first. This branch is for a tool-transport caller that
        # has no CLI-side empty-case wording of its own.
        return "no scheduled prompts"
    lines = []
    for e in entries:
        eta = datetime.fromtimestamp(e["target_ts"]).isoformat(timespec="seconds")
        prompt = e["prompt"]
        snip = prompt[:60] + "..." if len(prompt) > 60 else prompt
        lines.append("#%-4d  %-9s  %s   %s" % (e["id"], e["status"], eta, snip))
    return "\n".join(lines)


def _cmd_add(args):
    try:
        row = add(_slug(), args.when, args.prompt)
    except ValueError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    print("scheduled #%d for %s at %s"
          % (row["id"], row["cousin"],
             datetime.fromtimestamp(row["target_ts"]).isoformat(timespec="seconds")))
    return 0


def _cmd_list(args):
    slug = _slug()
    entries = list_entries(slug, include_fired=args.all,
                           limit=50 if args.all else None)
    if not entries:
        print("no jobs for %s%s"
              % (slug, " (incl history)" if args.all else " pending"))
        return 0
    print(format_entries(entries))
    return 0


def _cmd_cancel(args):
    slug = _slug()
    if cancel(args.id, slug=slug):
        print("cancelled #%d" % args.id)
        return 0
    print("no pending job #%d for %s" % (args.id, slug), file=sys.stderr)
    return 1


def _default_deliver(slug, prompt):
    """Hand a due prompt to the cousin through the delivery facade.
    False (skipped at a menu, failed, a tmux cousin's `queued`) is an
    outcome the producer does not accept. A runner cousin is not waited
    on: the inbox put is the acceptance."""
    from cousin_lib import delivery

    root = FrameworkConfig.from_env()
    for cfg in root.list_cousins():
        if cfg.slug == slug:
            item = delivery.Item(thread_id=delivery.thread_id("schedule"),
                                 source="schedule", body=prompt)
            wait = not isinstance(delivery.backend_for(cfg.home),
                                  delivery.InboxBackend)
            return delivery.accepted(delivery.deliver(cfg.home, item, wait=wait),
                                     cfg.home)
    raise RuntimeError("no cousin %r under %s" % (slug, root.root))


def _cmd_tick(args):
    fired = tick(deliver=_default_deliver)
    print("fired %d job(s)" % fired)
    return 0


@traced_cli("cousin-schedule")
def schedule_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-schedule", description="one-shot prompt scheduler"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add")
    p.add_argument("when")
    p.add_argument("prompt")
    p = sub.add_parser("list")
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("cancel")
    p.add_argument("id", type=int)
    sub.add_parser("tick")
    args = parser.parse_args(argv)
    handler = {"add": _cmd_add, "list": _cmd_list,
               "cancel": _cmd_cancel, "tick": _cmd_tick}[args.cmd]
    try:
        FrameworkConfig.for_command()
        return handler(args)
    except MissingConfigError as err:
        print("cousin-schedule: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(schedule_main())
