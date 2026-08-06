"""One-shot prompt scheduler.

`cousin-schedule add` stores a prompt with a target time; a periodic
`cousin-schedule tick` (run by whatever timer the install prefers -
cron, a systemd timer, a heartbeat) fires everything due by handing the
prompt to the delivery seam. Jobs are scoped per cousin; the database
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


def _db_path():
    return FrameworkConfig.from_env().root / "data" / "scheduled.db"


def _db():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
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


def tick(*, now_ts=None, deliver):
    """Fire every pending job whose time has come and return how many
    fired. A job is marked fired only AFTER its delivery succeeded: a
    failed delivery keeps it pending for the next tick, because losing
    a scheduled prompt silently is the one unforgivable failure here."""
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
                deliver(cousin, prompt)
            except Exception as err:
                print("cousin-schedule: job #%d delivery failed,"
                      " kept pending: %s" % (job_id, err),
                      file=sys.stderr)
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


def _cmd_add(args):
    try:
        ts = parse_when(args.when)
    except ValueError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    if ts <= datetime.now().timestamp():
        print("error: target %s is in the past"
              % datetime.fromtimestamp(ts).isoformat(), file=sys.stderr)
        return 2
    if not args.prompt.strip():
        print("error: empty prompt", file=sys.stderr)
        return 2
    slug = _slug()
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO scheduled_jobs (cousin, target_ts, prompt,"
            " created_at) VALUES (?, ?, ?, ?)",
            (slug, ts, args.prompt, int(datetime.now().timestamp())),
        )
        conn.commit()
        print("scheduled #%d for %s at %s"
              % (cur.lastrowid, slug,
                 datetime.fromtimestamp(ts).isoformat(timespec="seconds")))
    finally:
        conn.close()
    return 0


def _cmd_list(args):
    slug = _slug()
    conn = _db()
    try:
        if args.all:
            rows = conn.execute(
                "SELECT id, target_ts, status, prompt FROM scheduled_jobs"
                " WHERE cousin=? ORDER BY target_ts DESC LIMIT 50",
                (slug,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, target_ts, status, prompt FROM scheduled_jobs"
                " WHERE cousin=? AND status='pending' ORDER BY target_ts",
                (slug,),
            ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("no jobs for %s%s"
              % (slug, " (incl history)" if args.all else " pending"))
        return 0
    for job_id, ts, status, prompt in rows:
        eta = datetime.fromtimestamp(ts).isoformat(timespec="seconds")
        snip = prompt[:60] + "..." if len(prompt) > 60 else prompt
        print("#%-4d  %-9s  %s   %s" % (job_id, status, eta, snip))
    return 0


def _cmd_cancel(args):
    slug = _slug()
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE scheduled_jobs SET status='cancelled'"
            " WHERE id=? AND cousin=? AND status='pending'",
            (args.id, slug),
        )
        conn.commit()
        affected = cur.rowcount
    finally:
        conn.close()
    if affected == 0:
        print("no pending job #%d for %s" % (args.id, slug),
              file=sys.stderr)
        return 1
    print("cancelled #%d" % args.id)
    return 0


def _default_deliver(slug, prompt):
    """Fire a prompt into the cousin's terminal as an OOC framework
    line via the injection module."""
    from cousin_lib.server.injection import TmuxInjector

    root = FrameworkConfig.from_env()
    for cfg in root.list_cousins():
        if cfg.slug == slug:
            injector = TmuxInjector(cfg.tmux_session)
            injector.inject("[cousin-schedule] %s" % prompt)
            return
    raise RuntimeError("no cousin %r under %s" % (slug, root.root))


def _cmd_tick(args):
    fired = tick(deliver=_default_deliver)
    print("fired %d job(s)" % fired)
    return 0


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
