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

from cousin_lib.crashpoint import crashpoint
from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.trace import traced_cli
from cousin_lib.sqlite_util import add_column, wal

_ACTIVE = ("running",)


def _db():
    path = FrameworkConfig.from_env().root / "data" / "jobs.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    wal(conn)
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
    add_column(conn, "jobs", "pgid", "INTEGER")
    # the runner pid's identity: its /proc start time in clock ticks and
    # the boot it ran in, so a pid reused later (or after a reboot) is
    # never taken for the job's own, whatever the wall clock did
    add_column(conn, "jobs", "start_ticks", "INTEGER")
    add_column(conn, "jobs", "boot_id", "TEXT")
    # 1: the owner asked to be told when the row closes (notify_owner)
    add_column(conn, "jobs", "notify", "INTEGER NOT NULL DEFAULT 0")
    # 1: cousin-job forks this row's command itself, so a row of its with no
    # pid past SPAWN_GRACE_S was never forked; a hook-tracked background
    # shell has a command and never a pid, and is not one (#286)
    add_column(conn, "jobs", "launched", "INTEGER NOT NULL DEFAULT 0")
    # the git commit the job ran against (--commit): a verdict such as a
    # test run is about a commit even when it builds no artifact
    add_column(conn, "jobs", "git_commit", "TEXT")
    conn.commit()
    return conn


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def register_job(*, kind, title, description="", spawned_by=None,
                 log_path=None, command=None, notify=False, launched=False,
                 commit=None):
    """Insert a running job row and return its id. `notify`: the owner
    gets one inbox row when the job closes (notify_owner). `commit`: the
    git commit the job runs against."""
    slug = spawned_by or CousinConfig.from_env().slug
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO jobs (spawned_by, kind, title, description,"
            " started_at, log_path, command, notify, launched, git_commit)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (slug, kind, title[:200], description[:500], _now(),
             log_path, command, 1 if notify else 0, 1 if launched else 0,
             commit or None),
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
    if before is not None and before["status"] in ("running", "lost") and row:
        record_job_result(dict(row))
        notify_owner(dict(row), was_lost=before["status"] == "lost")


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


def notify_owner(job, *, was_lost=False):
    """One inbox row to the owning cousin for a job that asked for it
    (`notify`) and ended: what it was, how it ended, where its log is.
    The cousin waiting on it needs no polling (#282). A job whose
    spawned_by names no cousin home is skipped. Never raises: the job is
    closed either way, and a failed delivery is the poll it replaced."""
    try:
        if not job.get("notify"):
            return False
        owner = str(job.get("spawned_by") or "")
        if not owner or "/" in owner or owner.startswith("."):
            return False
        home = FrameworkConfig.from_env().root / "cousins" / owner
        if not (home / "cousin.toml").is_file():
            return False
        from cousin_lib import delivery
        title = " ".join(str(job.get("title") or "").split())
        head = "[cousin-job] job #%s %s" % (job.get("id"), job.get("status"))
        if job.get("exit_code") is not None:
            head += " (exit %s)" % job["exit_code"]
        if was_lost:
            # the second notice for this job: the first said lost
            head += " (it was marked lost; this is how it really ended)"
        body = "%s: %s" % (head, title or "(untitled)")
        summary = " ".join(str(job.get("result_summary") or "").split())
        if summary:
            body += " - %s" % summary[:JOB_SUMMARY_CHARS]
        if job.get("log_path"):
            body += "\nlog: %s" % job["log_path"]
        # "framework": a reserved sender (delivery.FRAMEWORK_SENDERS), so a
        # peer can never pass for the job notice
        item = delivery.Item(thread_id=delivery.thread_id("system"), source="job",
                             body=body, sender="framework")
        wait = not isinstance(delivery.backend_for(home), delivery.InboxBackend)
        return delivery.accepted(delivery.deliver(home, item, wait=wait), home)
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


STATUSES = ("running", "done", "failed", "cancelled", "lost")
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


# -- the job's process group --------------------------------------------
#
# A shell job's runner leads its own process group, and every process
# the command starts inherits it, including children that outlive the
# wrapper (an `ssh host tail -F` behind a pipe). Closing a job reaps
# that group; a finished job whose group still has members is shown as
# a leak. A member counts only if it started at or after the job did,
# so a reused process-group id never reaches an unrelated process, and
# nothing is ever killed by name.

def _boot_time():
    try:
        with open("/proc/stat") as fh:
            for line in fh:
                if line.startswith("btime "):
                    return int(line.split()[1])
    except OSError:
        pass
    return None


def _started_epoch(job):
    try:
        return datetime.fromisoformat(job["started_at"]).timestamp()
    except (TypeError, ValueError, KeyError):
        return None


def group_members(pgid, since=None):
    """Pids whose process group is `pgid` and that started at or after
    `since` (epoch seconds; None skips that check)."""
    if not pgid:
        return []
    btime = _boot_time()
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    out = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return []
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as fh:
                stat = fh.read()
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        try:
            pgrp = int(fields[2])
            start_ticks = int(fields[19])
        except (IndexError, ValueError):
            continue
        if pgrp != pgid:
            continue
        if since is not None and btime is not None:
            if btime + start_ticks / hz < since - 2:
                continue
        out.append(int(name))
    return sorted(out)


def live_members(job):
    return group_members(job.get("pgid"), _started_epoch(job))


def boot_id():
    """This boot's id (/proc/sys/kernel/random/boot_id), or None."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def pid_start_ticks(pid):
    """A live pid's start time in clock ticks since boot (/proc/<pid>/stat
    field 22), or None when it is gone."""
    try:
        with open("/proc/%d/stat" % int(pid)) as fh:
            stat = fh.read()
        fields = stat[stat.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None                         # a zombie has exited: nothing runs
        return int(fields[19])
    except (OSError, ValueError, TypeError, IndexError):
        return None


def record_spawn(job_id, pid):
    """Store a spawned runner's pid as the job's pid and process group,
    with its start ticks and the boot id: the identity reap_lost checks."""
    conn = _db()
    try:
        conn.execute("UPDATE jobs SET pid=?, pgid=?, start_ticks=?, boot_id=? WHERE id=?",
                     (pid, pid, pid_start_ticks(pid), boot_id(), job_id))
        conn.commit()
    finally:
        conn.close()


def _leader_alive(job):
    """For a row that stored its runner's identity: True while that very
    process runs (same boot, same start ticks), False once it is gone;
    None for an older row that stored no identity."""
    if job.get("start_ticks") is None or not job.get("boot_id"):
        return None
    if job["boot_id"] != boot_id():
        return False                                    # it ran in an earlier boot
    return pid_start_ticks(job["pid"]) == job["start_ticks"]


def _groups():
    """{pgid: [(pid, start epoch)]} from one pass over /proc."""
    btime = _boot_time()
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    out = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return out
    for name in entries:
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as fh:
                stat = fh.read()
            fields = stat[stat.rfind(")") + 2:].split()
            if fields[0] == "Z":
                continue                        # a zombie runs nothing
            pgrp, ticks = int(fields[2]), int(fields[19])
        except (OSError, IndexError, ValueError):
            continue
        started = btime + ticks / hz if btime is not None else None
        out.setdefault(pgrp, []).append((int(name), started))
    return out


def _process_gone(job, groups):
    """True only when the job names a process and nothing of it runs. A
    row with its runner's identity is alive while that exact process
    runs; failing that (or for an older row), while its group has a member
    started since the job did, or its pid-only process still runs. A row
    with no pid has nothing to check and is never called gone."""
    if not job.get("pid"):
        return False
    leader = _leader_alive(job)
    if leader:
        return False
    since = _started_epoch(job)
    if job.get("pgid"):
        members = groups.get(job["pgid"], [])
        live = [p for p, started in members
                if since is None or started is None or started >= since - 2]
        if live and leader is None:
            return False
        if live and leader is False and job.get("boot_id") == boot_id():
            # the runner died but processes it started still run: not lost
            return False
        return True
    ticks = pid_start_ticks(job["pid"])
    if ticks is None:
        return True
    btime, hz = _boot_time(), (os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100)
    return not (since is None or btime is None or btime + ticks / hz >= since - 2)


LOST_NOTE = " [lost: its process is gone]"
# A row cousin-job forks itself (`launched`) records its pid right after
# the fork; one that still has none this long after it started was never
# forked (its starter died in between), so nothing will ever close it
# (#286, job.registered). Well
# past a live starter's worst case: each of its writes may wait out a 5 s
# busy timeout before the pid lands.
SPAWN_GRACE_S = 60
UNSPAWNED_NOTE = " [lost: its command was never started]"


def reap_lost():
    """Rows 'running' whose process is gone are marked 'lost' at once,
    not 24 hours later (reap_stale): a job's runner closes its own row
    before it exits, so a running row with no live process means the
    runner died without closing it. Returns the ids marked. A host with
    no /proc cannot tell, and marks nothing. Maintenance any reader may
    run, like reap_stale; one /proc pass however many rows it checks.
    Not final: a runner that does finish later still closes the row."""
    if not os.path.isdir("/proc/self"):
        return []
    conn = _db()
    try:
        unspawned = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE status='running' AND pid IS NULL"
            " AND launched=1")]
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM jobs WHERE status='running' AND pid IS NOT NULL")]
        marked, lost = [], []
        now = datetime.now(timezone.utc)
        for job in unspawned:
            try:
                age = (now - datetime.fromisoformat(job["started_at"])).total_seconds()
            except (TypeError, ValueError):
                continue
            if age < SPAWN_GRACE_S:
                continue
            cur = conn.execute(
                "UPDATE jobs SET status='lost', finished_at=?,"
                " result_summary=COALESCE(result_summary, '') || ?"
                " WHERE id=? AND status='running' AND pid IS NULL",
                (_now(), UNSPAWNED_NOTE, job["id"]))
            if cur.rowcount:
                marked.append(job["id"])
                lost.append(dict(job, result_summary=UNSPAWNED_NOTE.strip()))
        groups = _groups() if rows else None
        for job in rows:
            if not _process_gone(job, groups):
                continue
            cur = conn.execute(
                "UPDATE jobs SET status='lost', finished_at=?,"
                " result_summary=COALESCE(result_summary, '') || ?"
                " WHERE id=? AND status='running'",
                (_now(), LOST_NOTE, job["id"]))
            if cur.rowcount:
                marked.append(job["id"])
                lost.append(job)
        conn.commit()
    finally:
        conn.close()
    for job in lost:
        notify_owner(dict(job, status="lost",
                          result_summary=job.get("result_summary") or LOST_NOTE.strip()))
    return marked


def reap_lost_quietly():
    """reap_lost for a reader whose own answer must not fail on it (the
    console's stream, the job tool): an error is printed, never raised."""
    try:
        return reap_lost()
    except Exception as err:  # noqa: BLE001 - maintenance never costs the read
        print("cousin-job: lost-job check failed: %s" % err, file=sys.stderr, flush=True)
        return []


def reap_group(job, *, grace=3.0):
    """SIGTERM the job's live group members, SIGKILL whatever is left
    after `grace` seconds. Returns the pids that were alive."""
    members = live_members(job)
    if not members:
        return []
    for pid in members:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + grace
    while time.time() < deadline and live_members(job):
        time.sleep(0.1)
    for pid in live_members(job):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    return members


def _spawn_tracked(cmd, log_path, job_id, *, artifacts=(), commit=None, owner=None):
    """Fork the command as a detached background process with its
    output in log_path, and write its exit status back to the store
    when it finishes. Double fork: the runner is reparented to init so
    nothing waits on the CLI. Returns the runner's pid. `artifacts`
    (absolute paths) are recorded for the job on exit 0, before the row
    closes (_record_artifacts)."""
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
    # Runner: the leader of the job's own process group, which every
    # process the command starts inherits.
    os.close(write_fd)
    try:
        os.setpgid(0, 0)
    except OSError:
        pass
    try:
        fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                     0o644)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        os.close(fd)
        # where this run's output begins: the summary reads only past it
        started_at = os.fstat(1).st_size
        try:
            rc = subprocess.call(cmd)
        except Exception as err:
            rc = 127
            sys.stderr.write("[cousin-job] exec error: %s\n" % err)
        # The rc lands in the store directly - if this write is what
        # fails, the row stays 'running' and `list --active` shows it,
        # which is the loud version of that failure.
        crashpoint("job.exited")
        status = "done" if rc == 0 else "failed"
        # read before the artifact lines go in: the command's own last word
        summary = last_log_line(log_path, start=started_at)
        if rc == 0 and artifacts:
            missing = _record_artifacts(job_id, artifacts, commit, owner)
            crashpoint("job.artifacts_recorded")
            if missing:
                # exit 0 without the output it named is not a build to show
                status = "failed"
                summary = "exit 0, but not recorded: " + "; ".join(missing)
        current = get_job(job_id)
        # a row a reader wrongly marked lost is still this runner's to close
        if current and current["status"] in ("running", "lost"):
            finish_job(job_id, status=status, summary=summary, exit_code=rc)
    finally:
        os._exit(0)


LAST_LINE_PREFIX = "last log line: "
_LAST_LINE_CHARS = 200
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def last_log_line(log_path, start=0):
    """The command's last non-empty output line, as a launched job's
    summary when it ends (a cousin's own summary at done or fail replaces
    it): "last log line: OK (skipped=69)". A heuristic, labelled as one;
    the exit code stays the verdict. Only what follows `start` (the log's
    size when the command began) is read, so a reused log never lends
    this run an earlier run's line. Colour codes are dropped. Empty when
    the command printed nothing."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(start, fh.tell() - 8192))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return ""
    for line in reversed(tail.splitlines()):
        line = " ".join(_ANSI.sub("", line).split())
        if line:
            if len(line) > _LAST_LINE_CHARS:
                line = line[:_LAST_LINE_CHARS - 3] + "..."
            return LAST_LINE_PREFIX + line
    return ""


def _log_line(text):
    """One line into the job's log: fd 1 in the runner is the log file,
    whatever object sys.stdout has become in this process."""
    os.write(1, (text + "\n").encode("utf-8", "replace"))


def _record_artifacts(job_id, paths, commit, owner):
    """Each path as an artifact of the job (artifacts.add), a line per
    path in the job's log; returns what could not be recorded,
    "<path>: <why>" each."""
    from cousin_lib import artifacts
    missing = []
    for path in paths:
        try:
            row = artifacts.add(path, created_by=owner or "", job_id=job_id, git_commit=commit)
            _log_line("[cousin-job] artifact #%s recorded: %s sha256 %s"
                      % (row.get("id"), row.get("path"), str(row.get("sha256", ""))[:12]))
        except Exception as err:  # noqa: BLE001 - any failure is named, and the row still closes
            missing.append("%s: %s" % (path, err))
            _log_line("[cousin-job] artifact NOT recorded: %s: %s" % (path, err))
    return missing


def home_log_path(home, rel):
    """A `--home-log` value as an absolute path inside `home`, or
    PathRefused: never absolute, never `~`, never through `..`, never in
    `.secrets` (home_files.resolve_in, the console's own boundary)."""
    from cousin_lib.home_files import resolve_in
    return str(resolve_in(home, rel))


def _cmd_start(args):
    cfg = CousinConfig.from_env()
    slug = cfg.slug
    cmd = list(args.cmdline or [])
    if cmd and not cmd[0].strip():
        # an empty program would register a job that dies 127
        print("cousin-job: the command's program is empty", file=sys.stderr)
        return 2
    if cmd and cmd[0] != cmd[0].strip():
        # " echo" is no program on PATH either: the same 127
        print("cousin-job: the command's program %r starts or ends with"
              " whitespace" % cmd[0], file=sys.stderr)
        return 2
    if cmd and cmd[0].startswith("-"):
        # A program is never named like an option; refusing it keeps a
        # command line from ever standing in for cousin-job's own flags.
        print("cousin-job: the command's program %r must not start with '-'"
              % cmd[0], file=sys.stderr)
        return 2
    if getattr(args, "home_log", None):
        # The job tool's log: confined to the home before any row exists.
        from cousin_lib.home_files import PathRefused
        if args.log:
            print("cousin-job: give --log or --home-log, not both",
                  file=sys.stderr)
            return 2
        try:
            args.log = home_log_path(cfg.home, args.home_log)
            os.makedirs(os.path.dirname(args.log), exist_ok=True)
        except PathRefused as err:
            print("cousin-job: --home-log %r refused: %s (a path relative to"
                  " the home, outside .secrets)" % (args.home_log, err),
                  file=sys.stderr)
            return 2
    # An artifact names a file the command will build: absolute now, a
    # relative path from where this runs, which is where the command runs
    # and builds (the job tool launches from the cousin's home); and only
    # with a command to build it.
    artifacts = [os.path.abspath(os.path.expanduser(p)) for p in (getattr(args, "artifact", None) or [])]
    if artifacts and not cmd:
        print("cousin-job: --artifact needs a command that builds it", file=sys.stderr)
        return 2
    # --commit (or its older name --artifact-commit): what the job runs
    # against, on the row and on each artifact it records
    commit = (getattr(args, "artifact_commit", None) or "").strip() or None
    job_id = register_job(
        kind=args.kind, title=args.title, description=args.desc or "",
        spawned_by=slug, log_path=args.log,
        command=" ".join(cmd) if cmd else None,
        notify=bool(getattr(args, "notify", False)), launched=bool(cmd),
        commit=commit,
    )
    crashpoint("job.registered")
    log_path = args.log
    if cmd:
        # Absolute in the row, so the console finds a relative --log
        # wherever it runs; relative means relative to where this ran.
        log_path = (os.path.abspath(log_path) if log_path
                    else str(_default_log_path(job_id)))
        set_log_path(job_id, log_path)
        _write_log_header(log_path, args.kind, args.title,
                          "$ " + " ".join(cmd))
        pid = _spawn_tracked(cmd, log_path, job_id, artifacts=artifacts,
                             commit=commit, owner=slug)
        record_spawn(job_id, pid)
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


def close_refusal(job, slug):
    """Why `slug` may not close `job`, or None. Closing kills the job's
    process group and writes its result into the owner's memory, so a
    cousin closes only the jobs it started (the operator closes any job
    from the console)."""
    owner = str(job.get("spawned_by") or "")
    if owner == slug:
        return None
    return ("job #%d refused: it was started by %s, not by %s; a cousin"
            " closes only its own jobs (the operator closes any job from"
            " the console)" % (job["id"], owner or "nobody", slug))


def _caller_refusal(job):
    """close_refusal for the cousin COUSIN_HOME names; None in a shell
    with no cousin home, which acts for no cousin."""
    if not os.environ.get("COUSIN_HOME"):
        return None
    return close_refusal(job, CousinConfig.from_env().slug)


def _close_cmd(args, status):
    job = get_job(args.id)
    if not job:
        print("job #%d not found" % args.id, file=sys.stderr)
        return 1
    refused = _caller_refusal(job)
    if refused:
        print("cousin-job: %s" % refused, file=sys.stderr)
        return 3
    # The row is closed first, so a runner whose command dies from the
    # reap below finds it closed and writes nothing over it. Then the
    # processes end: a row that says done over a command still running
    # is exactly the leak this prevents.
    finish_job(args.id, status=status, summary=args.summary or "",
               exit_code=getattr(args, "exit_code", None))
    reaped = reap_group(job)
    if reaped:
        print("job #%d: stopped %d process(es) still running in its"
              " group: %s" % (args.id, len(reaped),
                              " ".join(map(str, reaped))))
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
    refused = _caller_refusal(job)
    if refused:
        print("cousin-job: %s" % refused, file=sys.stderr)
        return 3
    if job["pid"] and not job.get("pgid"):
        # A row from before process groups were recorded: the runner
        # pid is all there is.
        try:
            os.kill(job["pid"], signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    return _close_cmd(args, "cancelled")


def _cmd_list(args):
    reap_lost()
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
        leak = ""
        if j["status"] != "running" and j.get("pgid") and live_members(j):
            leak = "  [LEAK: processes still running; cousin-job cancel %d]" % j["id"]
        print("%4d  %-10s %-9s %-10s %s%s"
              % (j["id"], j["status"], j["kind"],
                 j["spawned_by"] or "-", (j["title"] or "")[:60], leak))
    return 0


def _cmd_show(args):
    reap_lost()
    job = get_job(args.id)
    if not job:
        print("job #%d not found" % args.id, file=sys.stderr)
        return 1
    job["live_processes"] = live_members(job)
    if args.json:
        print(json.dumps(job, indent=2, sort_keys=True, default=str))
    else:
        for key, value in job.items():
            print("  %-14s: %s" % (key, value))
        if job["status"] != "running" and job["live_processes"]:
            print("  LEAK: the job is %s but its processes still run;"
                  " cousin-job cancel %d stops them"
                  % (job["status"], job["id"]))
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
        reap_lost_quietly()         # a runner that died never closes the row
        time.sleep(1)


def _start_options(p, *, help_text=False):
    """`start`'s own options, the one set every parser of the start line
    reads, so the separated shape's check and the title-first re-parse
    keep no copies of their own."""
    p.add_argument("--desc")
    p.add_argument("--log")
    if help_text:
        p.add_argument("--home-log", metavar="REL",
                       help="log file relative to COUSIN_HOME, confined to it"
                            " (refused: absolute, ~, .., .secrets)")
    else:
        p.add_argument("--home-log")
    p.add_argument("--json", action="store_true")
    if help_text:
        p.add_argument("--notify", action="store_true",
                       help="when the job ends, put one row in your inbox with its status,"
                            " exit code and log, so you need not poll it")
    else:
        p.add_argument("--notify", action="store_true")
    if help_text:
        p.add_argument("--artifact", action="append", metavar="PATH",
                       help="a file the command builds (repeat for more), relative to"
                            " the working directory, where the command runs: on exit 0 it is recorded as a shared"
                            " artifact row of this job (cousin-artifact); one that is missing"
                            " then fails the job. A private path: cousin-artifact add --private")
        p.add_argument("--commit", "--artifact-commit", dest="artifact_commit", metavar="SHA",
                       help="the git commit the job runs against: kept on its row and on"
                            " each artifact it records (--artifact-commit is the older name)")
    else:
        p.add_argument("--artifact", action="append")
        p.add_argument("--commit", "--artifact-commit", dest="artifact_commit")


def _title_after_separator(argv):
    """True for the `start KIND [options] -- TITLE [CMD...]` shape, the one
    the job tool uses: every option comes before the first `--`, and the
    title after it. Decided by parsing only what precedes that `--`, with
    the same options (so a prefix such as --js counts the same way): no
    title there means the title came after it."""
    argv = list(argv)
    if "--" not in argv:
        return False
    head = argv[1:argv.index("--")]
    p = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    p.add_argument("kind", nargs="?")
    p.add_argument("title", nargs="?")
    _start_options(p)
    try:
        known, _rest = p.parse_known_args(head)
    except (argparse.ArgumentError, SystemExit):
        return False
    return known.kind is not None and known.title is None


def _reparse_start_remainder(args):
    """The legacy, title-first shape (`start KIND TITLE [options] [--]
    [CMD...]`), which operators type: argparse.REMAINDER hoovers
    cousin-job's own options into the to-be-forked command when they
    follow the title; re-parse them back onto args so register-only mode
    and `TITLE --log L -- CMD` keep working. Never used for the separated
    shape (_title_after_separator), where nothing after `--` is an option:
    that is the shape a model's arguments reach, so a command line from a
    model never sets cousin-job's --log."""
    cl = list(args.cmdline or [])
    if cl and cl[0].startswith("--") and cl[0] != "--":
        opts = argparse.ArgumentParser(add_help=False)
        _start_options(opts)
        try:
            known, rest = opts.parse_known_args(cl)
            args.desc = args.desc or known.desc
            args.log = args.log or known.log
            args.home_log = args.home_log or known.home_log
            args.json = args.json or known.json
            args.artifact = (args.artifact or []) + (known.artifact or []) or None
            args.artifact_commit = args.artifact_commit or known.artifact_commit
            args.notify = getattr(args, "notify", False) or known.notify
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
    _start_options(p, help_text=True)
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
    raw = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(raw)
    if args.cmd == "start" and not _title_after_separator(raw):
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
        FrameworkConfig.for_command()
        return handlers[args.cmd](args)
    except MissingConfigError as err:
        print("cousin-job: %s" % err, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(jobs_main())
