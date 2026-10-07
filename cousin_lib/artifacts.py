"""Build outputs as rows, not notes (#250): what a cousin built, where it
is, its checksum, the job that made it and the commit it was built from.

A cousin that hand-keeps "the image is at X, sha256 Y, from job Z,
commit W" in STATUS.md has four facts that go stale silently. A row here
keeps them in one place, and `verify` says whether the file is still the
one recorded: same checksum, changed, or gone.

The store is <root>/data/artifacts.db, the install's like jobs.db: every
cousin can read it. A path or a note is visible to the whole install, so
a cousin doing work it must keep private records nothing here, or only
an opaque label (`--note`), never the path of a private tree.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from cousin_lib.config import CousinConfig, FrameworkConfig, MissingConfigError
from cousin_lib.sqlite_util import wal
from cousin_lib.trace import traced_cli

CHUNK = 1 << 20


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
        " path       TEXT NOT NULL,"
        " sha256     TEXT NOT NULL,"
        " size       INTEGER NOT NULL,"
        " job_id     INTEGER,"
        " git_commit TEXT,"
        " note       TEXT)")
    conn.commit()
    return conn


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def add(path, *, created_by, job_id=None, git_commit=None, note=None):
    """Record a file as an artifact: its absolute path, sha256 and size,
    measured now. ValueError for a path that is not a regular file."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise ValueError("%s is not a file" % path)
    row = {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "created_by": created_by, "path": str(path), "sha256": sha256_of(path),
           "size": path.stat().st_size, "job_id": job_id, "git_commit": git_commit,
           "note": note}
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO artifacts (created_at, created_by, path, sha256, size, job_id,"
            " git_commit, note) VALUES (:created_at, :created_by, :path, :sha256, :size,"
            " :job_id, :git_commit, :note)", row)
        conn.commit()
        return dict(row, id=cur.lastrowid)
    finally:
        conn.close()


def get(artifact_id):
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_rows(*, created_by=None, path=None, job_id=None, limit=100):
    """Newest first; filter by owner, by a path (exact, or a prefix ending
    in /), or by the producing job."""
    where, args = ["1=1"], []
    if created_by:
        where.append("created_by=?")
        args.append(created_by)
    if path:
        if path.endswith("/"):
            where.append("path LIKE ?")
            args.append(path + "%")
        else:
            where.append("path=?")
            args.append(str(Path(path).expanduser().resolve()))
    if job_id is not None:
        where.append("job_id=?")
        args.append(job_id)
    conn = _db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM artifacts WHERE %s ORDER BY id DESC LIMIT ?" % " AND ".join(where),
            args + [limit])]
    finally:
        conn.close()


def verify(row):
    """`ok` (the file still has the recorded checksum), `changed` or
    `missing`."""
    path = Path(row["path"])
    if not path.is_file():
        return "missing"
    return "ok" if sha256_of(path) == row["sha256"] else "changed"


def _line(r, state=None):
    bits = ["#%d" % r["id"], r["created_at"][:16], r["created_by"], r["sha256"][:12],
            "%d B" % r["size"], r["path"]]
    if r.get("job_id"):
        bits.append("job #%d" % r["job_id"])
    if r.get("git_commit"):
        bits.append("commit %s" % r["git_commit"][:12])
    if state:
        bits.append(state.upper())
    if r.get("note"):
        bits.append("- %s" % r["note"])
    return "  ".join(bits)


@traced_cli("cousin-artifact")
def artifact_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-artifact",
        description="Build outputs as rows: path, sha256, size, the job that made it,"
                    " the commit it came from.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="record a file: its path, sha256 and size, now")
    a.add_argument("path")
    a.add_argument("--job", type=int, help="the job that produced it")
    a.add_argument("--commit", help="the commit it was built from")
    a.add_argument("--note", help="one line; visible to the whole install")
    a.add_argument("--json", action="store_true")
    ls = sub.add_parser("list", help="newest first")
    ls.add_argument("--mine", action="store_true")
    ls.add_argument("--path", help="a file, or a directory ending in /")
    ls.add_argument("--job", type=int)
    ls.add_argument("--verify", action="store_true", help="check each file's checksum now")
    ls.add_argument("--json", action="store_true")
    v = sub.add_parser("verify", help="is the file still the recorded one?")
    v.add_argument("id", type=int)
    args = parser.parse_args(argv)
    try:
        FrameworkConfig.for_command()
    except MissingConfigError as err:
        print("cousin-artifact: %s" % err, file=sys.stderr)
        return 2
    if args.cmd == "add":
        try:
            me = CousinConfig.from_env().slug
        except MissingConfigError:
            me = os.environ.get("USER") or "operator"
        try:
            row = add(args.path, created_by=me, job_id=args.job, git_commit=args.commit,
                      note=args.note)
        except (ValueError, OSError) as err:
            print("cousin-artifact: %s" % err, file=sys.stderr)
            return 2
        print(json.dumps(row, sort_keys=True) if args.json else _line(row))
        return 0
    if args.cmd == "list":
        mine = None
        if args.mine:
            try:
                mine = CousinConfig.from_env().slug
            except MissingConfigError as err:
                print("cousin-artifact: --mine needs COUSIN_HOME: %s" % err, file=sys.stderr)
                return 2
        rows = list_rows(created_by=mine, path=args.path, job_id=args.job)
        if args.verify:
            for r in rows:
                r["state"] = verify(r)
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            print("\n".join(_line(r, r.get("state")) for r in rows) or "(no artifacts)")
        return 0
    row = get(args.id)
    if row is None:
        print("cousin-artifact: no artifact #%d" % args.id, file=sys.stderr)
        return 1
    state = verify(row)
    print(_line(row, state))
    return 0 if state == "ok" else 1
