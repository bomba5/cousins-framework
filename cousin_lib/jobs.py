"""Job tracking: register and track sub-agents and long-running work.

The store is this module's own SQLite database at <root>/data/jobs.db.
Registering a job works with no service running anywhere - a UI that
wants to show jobs reads the same database; nothing here depends on
one. Identity and root come from the environment and fail loud.
"""
import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

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
    status is whatever the closer says it is. The first close of a
    running row to done or failed also lands in the owning cousin's
    raw memory as an L2 (tool) entry - see record_job_result."""
    conn = _db()
    try:
        before = conn.execute("SELECT status FROM jobs WHERE id=?",
                              (job_id,)).fetchone()
        conn.execute(
            "UPDATE jobs SET status=?, finished_at=?,"
            " result_summary=COALESCE(NULLIF(?, ''), result_summary),"
            " exit_code=COALESCE(?, exit_code)"
            " WHERE id=?",
            (status, _now(), summary[:500], exit_code, job_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
    finally:
        conn.close()
    if before is not None and before["status"] == "running" and row:
        record_job_result(dict(row))


# What a finished job leaves in its cousin's raw memory. The topic
# folds repeat runs of the same work: job:<title slug>.
JOB_SUMMARY_CHARS = 400
_TOPIC_SLUG = re.compile(r"[^a-z0-9]+")


def job_topic(title):
    slug = _TOPIC_SLUG.sub("-", str(title or "").lower()).strip("-")
    return "job:%s" % (slug[:60].rstrip("-") or "untitled")


def record_job_result(job):
    """An L2_TOOL raw entry in the owning cousin's home for a job that
    ended done or failed: what ran, how it ended (exit code when
    known), what it said. A job whose spawned_by names no cousin home
    under the root is skipped. Never raises: the job is closed either
    way."""
    try:
        if job.get("status") not in ("done", "failed"):
            return False
        owner = str(job.get("spawned_by") or "")
        if not owner or "/" in owner or owner.startswith("."):
            return False
        home = FrameworkConfig.from_env().root / "cousins" / owner
        if not (home / "cousin.toml").is_file():
            return False
        from cousin_lib import memory
        title = " ".join(str(job.get("title") or "").split())
        head = "job #%s %s" % (job.get("id"), job["status"])
        if job.get("exit_code") is not None:
            head += " (exit %s)" % job["exit_code"]
        summary = " ".join(str(job.get("result_summary") or "").split())
        content = "%s: %s" % (head, title or "(untitled)")
        if summary:
            content += " - %s" % summary[:JOB_SUMMARY_CHARS]
        return memory.record_event(
            home, "L2_TOOL", job_topic(title), content, "job",
            job_id=job.get("id"), status=job["status"],
            exit_code=job.get("exit_code"), kind=job.get("kind"))
    except Exception:  # noqa: BLE001 - the close already happened
        return False


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
              limit=100, kind=None, since_hours=None, running_first=False):
    conn = _db()
    try:
        where, args = ["1=1"], []
        if status:
            where.append("status=?")
            args.append(status)
        if spawned_by:
            where.append("spawned_by=?")
            args.append(spawned_by)
        if kind:
            where.append("kind=?")
            args.append(kind)
        if since_hours:
            cutoff = (datetime.now(timezone.utc)
                      - timedelta(hours=float(since_hours)))
            where.append("started_at >= ?")
            args.append(cutoff.isoformat(timespec="seconds"))
        if active_only:
            where.append("status IN (%s)"
                         % ",".join("?" * len(_ACTIVE)))
            args.extend(_ACTIVE)
        order = ("(status='running') DESC, id DESC" if running_first
                 else "id DESC")
        rows = conn.execute(
            "SELECT * FROM jobs WHERE %s ORDER BY %s LIMIT ?"
            % (" AND ".join(where), order),
            args + [limit],
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


STATUSES = ("running", "done", "failed", "cancelled")
_UPDATABLE = ("status", "result_summary", "exit_code", "title",
              "description")


def update_job(job_id, **fields):
    """Set any of status, result_summary, exit_code, title, description.
    A terminal status stamps finished_at. Returns the row, None for an
    unknown id; ValueError for no fields or a status outside STATUSES."""
    sets, args = [], []
    for key in _UPDATABLE:
        if key in fields and fields[key] is not None:
            value = fields[key]
            if key == "status" and value not in STATUSES:
                raise ValueError("status must be one of %s"
                                 % "|".join(STATUSES))
            if key in ("title", "description"):
                value = str(value)[:500]
            sets.append("%s=?" % key)
            args.append(value)
    if not sets:
        raise ValueError("no updatable field given")
    if "status" in fields and fields["status"] != "running":
        sets.append("finished_at=?")
        args.append(_now())
    conn = _db()
    try:
        cur = conn.execute("UPDATE jobs SET %s WHERE id=?"
                           % ", ".join(sets), args + [job_id])
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM jobs WHERE id=?",
                           (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def delete_job(job_id):
    """Remove a row; True when one went."""
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def reap_stale(*, max_age_hours=24):
    """Rows 'running' for longer than max_age_hours are marked failed
    with an auto-reap note; returns how many. Maintenance the store's
    owner accepts from any reader."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
              ).isoformat(timespec="seconds")
    conn = _db()
    try:
        cur = conn.execute(
            "UPDATE jobs SET status='failed', finished_at=?,"
            " result_summary=COALESCE(result_summary, '') || ?"
            " WHERE status='running' AND started_at < ?",
            (_now(), " [auto-reap: stale running > %dh]" % max_age_hours,
             cutoff))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _minted_log_dir():
    return FrameworkConfig.from_env().root / "data" / "job-logs"


def is_minted_log(path):
    """True when a log path sits inside the store's own log directory -
    the only logs a reader may remove along with a row."""
    if not path:
        return False
    try:
        return (os.path.realpath(path).startswith(
            os.path.realpath(_minted_log_dir()) + os.sep))
    except (OSError, TypeError):
        return False


def rotate(*, cap=1000):
    """Cap the table: the oldest FINISHED rows beyond cap go, with their
    minted log files; running rows are never rotated. Returns how many
    rows were removed."""
    conn = _db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        excess = total - cap
        if excess <= 0:
            return 0
        rows = conn.execute(
            "SELECT id, log_path FROM jobs WHERE status != 'running'"
            " ORDER BY id ASC LIMIT ?", (excess,)).fetchall()
        for row in rows:
            if is_minted_log(row["log_path"]):
                try:
                    os.unlink(row["log_path"])
                except OSError:
                    pass
        ids = [row["id"] for row in rows]
        if ids:
            conn.execute("DELETE FROM jobs WHERE id IN (%s)"
                         % ",".join("?" * len(ids)), ids)
            conn.commit()
        return len(ids)
    finally:
        conn.close()


class track_job:
    """Context manager for in-process tracking.

    Catches BaseException, not Exception: SystemExit and
    KeyboardInterrupt used to propagate straight past the close and
    leave rows stuck at 'running' forever. A clean sys.exit(0) unwinds
    as SystemExit too and is a SUCCESS - mislabeling it as failed just
    because it unwinds that way was the bug this class carries the
    scar of. Everything is marked, then re-raised unchanged.

    Every tracked job gets a readable log in the store's log directory:
    a header with the description, whatever the caller adds with
    log(), and the outcome. Logging is best-effort and never fails the
    job it describes."""

    def __init__(self, kind, title, description="", spawned_by=None):
        self.kind = kind
        self.title = title
        self.description = description
        self.spawned_by = spawned_by
        self.job_id = None
        self.summary = ""
        self.log_path = None

    def log(self, text):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write("%s  %s\n" % (
                    datetime.now().strftime("%H:%M:%S"), text))
        except OSError:
            pass

    def __enter__(self):
        self.job_id = register_job(
            kind=self.kind, title=self.title,
            description=self.description, spawned_by=self.spawned_by,
        )
        try:
            path = _default_log_path(self.job_id)
            path.write_text("# %s: %s\n# started %s\n\n%s\n\n" % (
                self.kind, self.title,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                (self.description or "").rstrip()))
            set_log_path(self.job_id, path)
            self.log_path = path
        except OSError:
            self.log_path = None
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            self.log("done: %s" % (self.summary or "ok"))
        elif isinstance(exc, SystemExit) and (exc.code or 0) == 0:
            self.log("done: %s" % (self.summary or "ok (exit 0)"))
        else:
            self.log("FAILED: %s: %s" % (exc_type.__name__, exc))
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


def _write_log_header(path, kind, title, detail):
    """The first lines of a minted job log: what the job is and when it
    started, so the log reads on its own."""
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("# %s: %s\n# started %s\n\n%s\n\n" % (
            kind, title, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            (detail or "").rstrip()))


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
        _write_log_header(log_path, args.kind, args.title,
                          "$ " + " ".join(cmd))
        pid = _spawn_tracked(cmd, log_path, job_id)
        conn = _db()
        try:
            conn.execute("UPDATE jobs SET pid=? WHERE id=?",
                         (pid, job_id))
            conn.commit()
        finally:
            conn.close()
    elif not log_path:
        # A job with no process of its own (a hand-registered subagent,
        # a manual step) still gets a log: its header now, its outcome
        # at done/fail/cancel.
        try:
            log_path = str(_default_log_path(job_id))
            _write_log_header(log_path, args.kind, args.title,
                              args.desc or "")
            set_log_path(job_id, log_path)
        except OSError:
            log_path = None
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
    if job.get("log_path") and is_minted_log(job["log_path"]):
        try:
            with open(job["log_path"], "a", encoding="utf-8") as fh:
                fh.write("\n## %s %s\n%s\n" % (
                    status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    args.summary or ""))
        except OSError:
            pass
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
