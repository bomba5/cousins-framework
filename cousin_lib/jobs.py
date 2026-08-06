"""Job tracking: register and track sub-agents and long-running work.

The store is this module's own SQLite database at <root>/data/jobs.db.
Registering a job works with no service running anywhere - a UI that
wants to show jobs reads the same database; nothing here depends on
one. Identity and root come from the environment and fail loud.
"""
import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.trace import traced_cli

_ACTIVE = ("running",)


def _db():
    path = FrameworkConfig.from_env().root / "data" / "jobs.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS jobs ("
        " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
        " spawned_by     TEXT NOT NULL,"
        " kind           TEXT NOT NULL,"
        " title          TEXT NOT NULL,"
        " description    TEXT,"
        " status         TEXT NOT NULL DEFAULT 'running',"
        " started_at     TEXT NOT NULL,"
        " finished_at    TEXT,"
        " exit_code      INTEGER,"
        " result_summary TEXT,"
        " log_path       TEXT,"
        " pid            INTEGER,"
        " command        TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
    )
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def register_job(*, kind, title, description="", spawned_by=None,
                 log_path=None, command=None):
    """Insert a running job row and return its id."""
    slug = spawned_by or CousinConfig.from_env().slug
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO jobs (spawned_by, kind, title, description,"
            " started_at, log_path, command)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, kind, title[:200], description[:500], _now(),
             log_path, command),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def finish_job(job_id, *, status="done", summary="", exit_code=None):
    """Close a job row. Idempotent by last-write-wins; the terminal
    status is whatever the closer says it is."""
    conn = _db()
    try:
        conn.execute(
            "UPDATE jobs SET status=?, finished_at=?,"
            " result_summary=COALESCE(NULLIF(?, ''), result_summary),"
            " exit_code=COALESCE(?, exit_code)"
            " WHERE id=?",
            (status, _now(), summary[:500], exit_code, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def set_log_path(job_id, log_path):
    conn = _db()
    try:
        conn.execute("UPDATE jobs SET log_path=? WHERE id=?",
                     (str(log_path), job_id))
        conn.commit()
    finally:
        conn.close()


def get_job(job_id):
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_jobs(*, status=None, spawned_by=None, active_only=False,
              limit=100):
    conn = _db()
    try:
        where, args = ["1=1"], []
        if status:
            where.append("status=?")
            args.append(status)
        if spawned_by:
            where.append("spawned_by=?")
            args.append(spawned_by)
        if active_only:
            where.append("status IN (%s)"
                         % ",".join("?" * len(_ACTIVE)))
            args.extend(_ACTIVE)
        rows = conn.execute(
            "SELECT * FROM jobs WHERE %s ORDER BY id DESC LIMIT ?"
            % " AND ".join(where),
            args + [limit],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


class track_job:
    """Context manager for in-process tracking.

    Catches BaseException, not Exception: SystemExit and
    KeyboardInterrupt used to propagate straight past the close and
    leave rows stuck at 'running' forever. A clean sys.exit(0) unwinds
    as SystemExit too and is a SUCCESS - mislabeling it as failed just
    because it unwinds that way was the bug this class carries the
    scar of. Everything is marked, then re-raised unchanged."""

    def __init__(self, kind, title, description="", spawned_by=None):
        self.kind = kind
        self.title = title
        self.description = description
        self.spawned_by = spawned_by
        self.job_id = None
        self.summary = ""

    def __enter__(self):
        self.job_id = register_job(
            kind=self.kind, title=self.title,
            description=self.description, spawned_by=self.spawned_by,
        )
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            finish_job(self.job_id, status="done",
                       summary=self.summary or "ok")
        elif isinstance(exc, SystemExit) and (exc.code or 0) == 0:
            finish_job(self.job_id, status="done",
                       summary=self.summary or "ok (exit 0)")
        else:
            finish_job(
                self.job_id, status="failed",
                summary="%s: %s" % (exc_type.__name__, str(exc)[:180]),
            )
        return False


def _default_log_path(job_id):
    log_dir = FrameworkConfig.from_env().root / "data" / "job-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / ("job-%d.log" % job_id)


def _spawn_tracked(cmd, log_path, job_id):
    """Fork the command as a detached background process with its
    output in log_path, and write its exit status back to the store
    when it finishes. Double fork: the runner is reparented to init so
    nothing waits on the CLI. Returns the runner's pid."""
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid != 0:
        os.close(write_fd)
        with os.fdopen(read_fd) as fh:
            line = fh.readline().strip()
        os.waitpid(pid, 0)
        return int(line or 0)
    # Intermediate child.
    os.close(read_fd)
    os.setsid()
    runner_pid = os.fork()
    if runner_pid != 0:
        os.write(write_fd, ("%d\n" % runner_pid).encode())
        os.close(write_fd)
        os._exit(0)
    # Runner.
    os.close(write_fd)
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                     0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        try:
            rc = subprocess.call(cmd)
        except Exception as err:
            rc = 127
            sys.stderr.write("[cousin-job] exec error: %s\n" % err)
        # The rc lands in the store directly - if this write is what
        # fails, the row stays 'running' and `list --active` shows it,
        # which is the loud version of that failure.
        status = "done" if rc == 0 else "failed"
        current = get_job(job_id)
        if current and current["status"] == "running":
            finish_job(job_id, status=status, exit_code=rc)
    finally:
        os._exit(0)


def _cmd_start(args):
    slug = CousinConfig.from_env().slug
    cmd = list(args.cmdline or [])
    job_id = register_job(
        kind=args.kind, title=args.title, description=args.desc or "",
        spawned_by=slug, log_path=args.log,
        command=" ".join(cmd) if cmd else None,
    )
    log_path = args.log
    if cmd:
        log_path = log_path or str(_default_log_path(job_id))
        set_log_path(job_id, log_path)
        pid = _spawn_tracked(cmd, log_path, job_id)
        conn = _db()
        try:
            conn.execute("UPDATE jobs SET pid=? WHERE id=?",
                         (pid, job_id))
            conn.commit()
        finally:
            conn.close()
    if args.json:
        print(json.dumps({"job_id": job_id, "log_path": log_path}))
    else:
        print(job_id)
    return 0


def _close_cmd(args, status):
    job = get_job(args.id)
    if not job:
        print("job #%d not found" % args.id, file=sys.stderr)
        return 1
    finish_job(args.id, status=status, summary=args.summary or "",
               exit_code=getattr(args, "exit_code", None))
    print("job #%d %s" % (args.id, status))
    return 0


def _cmd_cancel(args):
    job = get_job(args.id)
    if not job:
        print("job #%d not found" % args.id, file=sys.stderr)
        return 1
    if job["pid"]:
        try:
            os.kill(job["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    return _close_cmd(args, "cancelled")


def _cmd_list(args):
    spawned_by = CousinConfig.from_env().slug if args.mine else None
    jobs = list_jobs(status=args.status, spawned_by=spawned_by,
                     active_only=args.active)
    if args.json:
        print(json.dumps(jobs, indent=2, sort_keys=True, default=str))
        return 0
    if not jobs:
        print("(no jobs)")
        return 0
    print("%4s  %-10s %-9s %-10s %s"
          % ("ID", "STATUS", "KIND", "OWNER", "TITLE"))
    for j in jobs:
        print("%4d  %-10s %-9s %-10s %s"
              % (j["id"], j["status"], j["kind"],
                 j["spawned_by"] or "-", (j["title"] or "")[:60]))
    return 0


def _cmd_show(args):
    job = get_job(args.id)
    if not job:
        print("job #%d not found" % args.id, file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(job, indent=2, sort_keys=True, default=str))
    else:
        for key, value in job.items():
            print("  %-14s: %s" % (key, value))
    return 0


def _cmd_tail(args):
    job = get_job(args.id)
    if not job:
        print("job #%d not found" % args.id, file=sys.stderr)
        return 1
    if not job["log_path"] or not os.path.exists(job["log_path"]):
        print("job #%d has no log" % args.id, file=sys.stderr)
        return 1
    def last_lines():
        with open(job["log_path"], errors="replace") as fh:
            return fh.readlines()[-args.lines:]
    print("".join(last_lines()), end="")
    if not args.follow:
        return 0
    size = os.path.getsize(job["log_path"])
    while True:
        current = get_job(args.id)
        new_size = os.path.getsize(job["log_path"])
        if new_size > size:
            with open(job["log_path"], errors="replace") as fh:
                fh.seek(size)
                sys.stdout.write(fh.read())
                sys.stdout.flush()
            size = new_size
        if current["status"] != "running":
            print("--- job #%d %s ---" % (args.id, current["status"]))
            return 0
        time.sleep(1)


def _reparse_start_remainder(args):
    """argparse.REMAINDER hoovers cousin-job's own options into the
    to-be-forked command when they follow the title without a `--`;
    re-parse them back onto args so register-only mode keeps working."""
    cl = list(args.cmdline or [])
    if cl and cl[0].startswith("--") and cl[0] != "--":
        opts = argparse.ArgumentParser(add_help=False)
        opts.add_argument("--desc")
        opts.add_argument("--log")
        opts.add_argument("--json", action="store_true")
        try:
            known, rest = opts.parse_known_args(cl)
            args.desc = args.desc or known.desc
            args.log = args.log or known.log
            args.json = args.json or known.json
            cl = rest
        except SystemExit:
            pass
    if cl and cl[0] == "--":
        cl = cl[1:]
    args.cmdline = cl


@traced_cli("cousin-job")
def jobs_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-job",
        description="register and track sub-agents and background jobs",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("start")
    p.add_argument("kind", choices=["subagent", "shell", "build", "other"])
    p.add_argument("title")
    p.add_argument("--desc")
    p.add_argument("--log")
    p.add_argument("--json", action="store_true")
    p.add_argument("cmdline", nargs=argparse.REMAINDER)
    for name in ("done", "fail", "cancel"):
        p = sub.add_parser(name)
        p.add_argument("id", type=int)
        p.add_argument("summary", nargs="?")
        if name == "fail":
            p.add_argument("--exit", dest="exit_code", type=int)
    p = sub.add_parser("list")
    p.add_argument("--status")
    p.add_argument("--mine", action="store_true")
    p.add_argument("--active", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("show")
    p.add_argument("id", type=int)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("tail")
    p.add_argument("id", type=int)
    p.add_argument("--lines", type=int, default=40)
    p.add_argument("--follow", "-f", action="store_true")
    args = parser.parse_args(argv)
    if args.cmd == "start":
        _reparse_start_remainder(args)
    handlers = {
        "start": _cmd_start,
        "done": lambda a: _close_cmd(a, "done"),
        "fail": lambda a: _close_cmd(a, "failed"),
        "cancel": _cmd_cancel,
        "list": _cmd_list,
        "show": _cmd_show,
        "tail": _cmd_tail,
    }
    try:
        return handlers[args.cmd](args)
    except MissingConfigError as err:
        print("cousin-job: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(jobs_main())
