"""Build outputs as rows, not notes (#250): what a cousin built, where it
is, its checksum, the job that made it and the commit it was built from.

A cousin that hand-keeps "the image is at X, sha256 Y, from job Z,
commit W" in STATUS.md has four facts that go stale silently. A row here
keeps them in one place, and `verify` says whether the file is still the
one recorded: same checksum, changed, or gone.

The store is <root>/data/artifacts.db, the install's like jobs.db: every
cousin can read it. A private row (`--private --label L`) keeps only the
label there; its path stays in the owner's home
(<home>/data/artifacts-private.json), so only the owner can verify it. A
remote row (`--host H`) records a file on another machine with the
checksum and size measured there; verifying it runs `sha256sum` on that
host over ssh, and only when asked (`--remote`).
"""
import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib import jobs
from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.sqlite_util import wal
from cousin_lib.trace import traced_cli

CHUNK = 1 << 20
SSH_TIMEOUT = 600
LIST_CAP = 100
_SHA = re.compile(r"^[0-9a-f]{64}$")


def _db():
    path = FrameworkConfig.from_env().root / "data" / "artifacts.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    wal(conn)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS artifacts ("
        " id         INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT NOT NULL,"
        " created_by TEXT NOT NULL,"
        " path       TEXT,"
        " host       TEXT,"
        " label      TEXT,"
        " private    INTEGER NOT NULL DEFAULT 0,"
        " sha256     TEXT NOT NULL,"
        " size       INTEGER NOT NULL,"
        " mtime      REAL,"
        " job_id     INTEGER,"
        " git_commit TEXT,"
        " note       TEXT)")
    conn.commit()
    return conn


def _private_file(home):
    return Path(home) / "data" / "artifacts-private.json"


def _private_paths(home):
    try:
        return json.loads(_private_file(home).read_text())
    except (OSError, ValueError):
        return {}


def _save_private(home, paths):
    path = _private_file(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(paths, indent=2, sort_keys=True))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _check_host(host):
    if not host or host.startswith("-") or any(c.isspace() for c in host):
        raise ValueError("bad host %r" % host)


def _measure(path):
    """(sha256, size, mtime) of a local file that held still while it was
    hashed. ValueError for a file still being written."""
    before = path.stat()
    digest = sha256_of(path)
    after = path.stat()
    if (before.st_size, before.st_mtime) != (after.st_size, after.st_mtime):
        raise ValueError("%s is still changing; record it when the build is done" % path)
    return digest, after.st_size, after.st_mtime


def add(path, *, created_by, job_id=None, git_commit=None, note=None, host=None,
        sha256=None, size=None, private=False, label=None, home=None):
    """Record a file as an artifact. A local file: its resolved absolute
    path (a symlink is stored as its target), sha256, size and mtime,
    measured now. A remote one (`host`): the path as given on that host
    (absolute) with the sha256 and size measured there. A private row
    (`private`, needs `label` and the owner's `home`) keeps the path and
    host in the owner's home, not in the shared row. ValueError for
    anything that cannot be recorded."""
    if job_id is not None and jobs.get_job(job_id) is None:
        raise ValueError("no job #%d" % job_id)
    if private and not (label and home):
        raise ValueError("a private row needs --label and a cousin home")
    mtime = None
    if host:
        _check_host(host)
        if not str(path).startswith("/"):
            raise ValueError("a remote path must be absolute: %s" % path)
        sha256 = (sha256 or "").lower()
        if not _SHA.match(sha256) or size is None or size < 0:
            raise ValueError("a remote row needs --sha256 (64 hex) and --size, measured on %s" % host)
        path = str(path)
    else:
        if sha256 is not None or size is not None:
            raise ValueError("--sha256 and --size are for a remote row (--host); a local file is measured")
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise ValueError("%s is not a file" % path)
        sha256, size, mtime = _measure(path)
        path = str(path)
    row = {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "created_by": created_by, "path": None if private else path,
           "host": None if private else host, "label": label, "private": int(bool(private)),
           "sha256": sha256, "size": size, "mtime": mtime, "job_id": job_id,
           "git_commit": git_commit, "note": note}
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO artifacts (created_at, created_by, path, host, label, private,"
            " sha256, size, mtime, job_id, git_commit, note) VALUES (:created_at,"
            " :created_by, :path, :host, :label, :private, :sha256, :size, :mtime,"
            " :job_id, :git_commit, :note)", row)
        row_id = cur.lastrowid
        if private:
            paths = _private_paths(home)
            paths[str(row_id)] = {"path": path, "host": host}
            _save_private(home, paths)
        conn.commit()
        return dict(row, id=row_id)
    finally:
        conn.close()


def get(artifact_id):
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def remove(artifact_id, *, by=None, home=None):
    """Drop a row: its owner (`by`), or anyone when `by` is None (the
    operator). A private row's path leaves the owner's home too. False
    when there is no such row; PermissionError for someone else's."""
    row = get(artifact_id)
    if row is None:
        return False
    if by is not None and row["created_by"] != by:
        raise PermissionError("#%d is %s's" % (artifact_id, row["created_by"]))
    conn = _db()
    try:
        conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
        conn.commit()
    finally:
        conn.close()
    if row["private"] and home and by == row["created_by"]:
        paths = _private_paths(home)
        if paths.pop(str(artifact_id), None) is not None:
            _save_private(home, paths)
    return True


def list_rows(*, created_by=None, path=None, job_id=None, limit=LIST_CAP):
    """Newest first, at most LIST_CAP; filter by owner, by a path (a file,
    or a directory ending in /, both resolved like `add`), or by the
    producing job. A path filter matches local, non-private rows only."""
    where, args = ["1=1"], []
    if created_by:
        where.append("created_by=?")
        args.append(created_by)
    if path:
        resolved = str(Path(path).expanduser().resolve())
        where.append("host IS NULL")
        if path.endswith("/"):
            prefix = resolved.rstrip("/") + "/"
            where.append("substr(path, 1, ?)=?")
            args += [len(prefix), prefix]
        else:
            where.append("path=?")
            args.append(resolved)
    if job_id is not None:
        where.append("job_id=?")
        args.append(job_id)
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM artifacts WHERE %s ORDER BY id DESC LIMIT ?" % " AND ".join(where),
            args + [max(1, min(int(limit), LIST_CAP))])]
    finally:
        conn.close()


def _where(row, *, me=None, home=None):
    """(path, host) for verifying a row, or (None, state) when this
    caller can't: a private row resolves only for its owner."""
    if not row["private"]:
        return row["path"], row["host"]
    if me is None or me != row["created_by"] or not home:
        return None, "private"
    entry = _private_paths(home).get(str(row["id"]))
    if not entry:
        return None, "unknown"
    return entry["path"], entry.get("host")


def _remote_sha(host, path):
    _check_host(host)
    try:
        out = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host,
             "sha256sum -- " + shlex.quote(path)],
            capture_output=True, text=True, timeout=SSH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None, "unreachable"
    if out.returncode != 0:
        gone = "No such file" in out.stderr
        return None, "missing" if gone else "unreachable"
    return (out.stdout.split() or [""])[0], None


def verify(row, *, quick=False, remote=False, me=None, home=None):
    """A row's state now. A full check hashes the file: `ok`, `changed`,
    `missing` or `unreadable`. A quick one only stats it: `unchanged`
    (same size and mtime), `touched` (same size, new mtime: hash to tell),
    `changed` (other size), `missing`, `unreadable`. A remote row is
    `unverified` unless `remote` (then hashed over ssh; `unreachable` when
    ssh fails); a private row is `private` unless `me` is its owner with
    `home` set."""
    path, host = _where(row, me=me, home=home)
    if path is None:
        return host
    if host:
        if not remote or quick:
            return "unverified"
        digest, state = _remote_sha(host, path)
        return state or ("ok" if digest == row["sha256"] else "changed")
    path = Path(path)
    try:
        if not path.is_file():
            return "missing"
        st = path.stat()
        if quick:
            if st.st_size != row["size"]:
                return "changed"
            return "unchanged" if row.get("mtime") == st.st_mtime else "touched"
        return "ok" if sha256_of(path) == row["sha256"] else "changed"
    except OSError:
        return "unreadable"


def _place(r):
    if r["private"]:
        return "[private] %s" % r["label"]
    where = "%s:%s" % (r["host"], r["path"]) if r["host"] else r["path"]
    return "%s (%s)" % (where, r["label"]) if r.get("label") else where


def _line(r, state=None):
    bits = ["#%d" % r["id"], r["created_at"][:16], r["created_by"], r["sha256"][:12],
            "%d B" % r["size"], _place(r)]
    if r.get("job_id"):
        bits.append("job #%d" % r["job_id"])
    if r.get("git_commit"):
        bits.append("commit %s" % r["git_commit"][:12])
    if state:
        bits.append(state.upper())
    if r.get("note"):
        bits.append("- %s" % r["note"])
    return "  ".join(bits)


def _me():
    """(slug, home) of the calling cousin, or (None, None) outside one."""
    try:
        cfg = CousinConfig.from_env()
    except MissingConfigError:
        return None, None
    return cfg.slug, cfg.home


@traced_cli("cousin-artifact")
def artifact_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-artifact",
        description="Build outputs as rows: path, sha256, size, the job that made it,"
                    " the commit it came from.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="record a file: its path, sha256 and size, now")
    a.add_argument("path", help="a local file, or an absolute path on --host")
    a.add_argument("--host", help="the file is on this ssh host; give --sha256 and --size measured there")
    a.add_argument("--sha256", help="with --host")
    a.add_argument("--size", type=int, help="with --host, in bytes")
    a.add_argument("--private", action="store_true",
                   help="keep the path in your home; the shared row shows only --label")
    a.add_argument("--label", help="a short name for the row (required with --private)")
    a.add_argument("--job", type=int, help="the job that produced it")
    a.add_argument("--commit", help="the commit it was built from")
    a.add_argument("--note", help="one line; visible to the whole install")
    a.add_argument("--json", action="store_true")
    ls = sub.add_parser("list", help="newest first, at most %d" % LIST_CAP)
    ls.add_argument("--mine", action="store_true")
    ls.add_argument("--path", help="a file, or a directory ending in /")
    ls.add_argument("--job", type=int)
    ls.add_argument("--verify", action="store_true", help="check each file's checksum now")
    ls.add_argument("--remote", action="store_true", help="with --verify: hash remote rows over ssh")
    ls.add_argument("--json", action="store_true")
    v = sub.add_parser("verify", help="is the file still the recorded one? (exit 0 only for ok)")
    v.add_argument("id", type=int)
    v.add_argument("--remote", action="store_true", help="hash a remote row over ssh")
    r = sub.add_parser("rm", help="drop a row (your own; any, outside a cousin)")
    r.add_argument("id", type=int)
    args = parser.parse_args(argv)
    try:
        FrameworkConfig.for_command()
    except MissingConfigError as err:
        print("cousin-artifact: %s" % err, file=sys.stderr)
        return 2
    me, home = _me()
    if args.cmd == "add":
        try:
            row = add(args.path, created_by=me or os.environ.get("USER") or "operator",
                      job_id=args.job, git_commit=args.commit, note=args.note,
                      host=args.host, sha256=args.sha256, size=args.size,
                      private=args.private, label=args.label, home=home)
        except (ValueError, OSError) as err:
            print("cousin-artifact: %s" % err, file=sys.stderr)
            return 2
        print(json.dumps(row, sort_keys=True) if args.json else _line(row))
        return 0
    if args.cmd == "list":
        if args.mine and me is None:
            print("cousin-artifact: --mine needs COUSIN_HOME", file=sys.stderr)
            return 2
        rows = list_rows(created_by=me if args.mine else None, path=args.path, job_id=args.job)
        if args.verify:
            for row in rows:
                row["state"] = verify(row, remote=args.remote, me=me, home=home)
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            print("\n".join(_line(row, row.get("state")) for row in rows) or "(no artifacts)")
        return 0
    if args.cmd == "rm":
        try:
            gone = remove(args.id, by=me, home=home)
        except PermissionError as err:
            print("cousin-artifact: %s" % err, file=sys.stderr)
            return 2
        if not gone:
            print("cousin-artifact: no artifact #%d" % args.id, file=sys.stderr)
            return 1
        print("removed #%d" % args.id)
        return 0
    row = get(args.id)
    if row is None:
        print("cousin-artifact: no artifact #%d" % args.id, file=sys.stderr)
        return 1
    state = verify(row, remote=args.remote, me=me, home=home)
    print(_line(row, state))
    return 0 if state == "ok" else 1
