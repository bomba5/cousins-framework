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

    `deliver(cousin, prompt)` receives the RAW prompt; adding the
    provenance prefix is the deliverer's job (`_default_deliver` here,
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
            "SELECT id, cousin, prompt FROM scheduled_jobs"
            " WHERE status='pending' AND target_ts<=? ORDER BY target_ts",
            (now_ts,),
        ).fetchall()
        fired = 0
        for job_id, cousin, prompt in rows:
            try:
                if deliver(cousin, prompt) is False:
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
        return handler(args)
    except MissingConfigError as err:
        print("cousin-schedule: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(schedule_main())
