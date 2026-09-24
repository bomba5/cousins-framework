"""Backup snapshots of a cousin home: databases via VACUUM INTO,
memory and the core markdown files as plain copies.

VACUUM INTO gives a consistent copy of a database another process
holds open in WAL mode; a file copy of the same database can be torn.
Rebuildable search indexes under memory/ are not memory and are not
snapshotted - a restore regenerates them on the first search.

The runner's event stream (data/stream/*.jsonl, one append-only file
per runner session) is copied too, cut in the copy to its last complete
line: a writer may be mid-line, and a torn tail can end inside a UTF-8
character, which EventStream.tail() (a text-mode reader) cannot decode.
The small state files a runner restores from are plain copies
(RUNNER_STATE): the session ids it resumes (runner-session*.json, one
per session kind), its generation count (generation.txt), and the
cursors and window of its per-turn mining and proposals
(extract-cursor.json, propose-cursor.json, proposals.json). Without
them a restored home starts a fresh session, resets its generation and
re-mines turns it already mined.

Order matters while a runner is live. data/inbox.db is snapshotted
FIRST, then the other databases, then the streams, then the runner's
state files. A turn commits its reply (chat.db) before it closes its row
(inbox.db), so a row `done` in the copy had its reply committed before
the copy of chat.db began; a row still `claimed` in the copy is
requeued on restore and at worst answered twice. At-least-once, never
lost. The reverse order could restore a `done` row with no reply.

The destination is an argument, never a default: a backup path in code
is somebody's disk. The source also committed and pushed the snapshot
directory to git; that is the operator's choice of what to do with a
directory of files, not this module's.
"""
import argparse
import os
import shutil
import sqlite3
import sys
from datetime import date
from pathlib import Path

from cousin_lib.config import CousinConfig, MissingConfigError
from cousin_lib.trace import traced_cli

CORE_FILES = ("MEMORY.md", "STATUS.md", "CLAUDE.md")
# Index artifacts memory search rebuilds on demand; never memory.
MEMORY_SKIP = ("fts_index.db", "fts_index.db-wal", "fts_index.db-shm",
               "vectors.db",
               # A home that has not migrated yet still has the
               # JSON index; an index of either shape is a rebuildable
               # cache and never belongs in a snapshot.
               "embeddings.json")
# The runner's event stream, relative to data/ (runner/stream.py).
STREAM_GLOB = "stream/*.jsonl"
STREAM_BLOCK = 64 * 1024
# Snapshotted before every other database (see the module docstring).
INBOX_DB = "inbox.db"
# The runner's small state files, relative to data/: the session ids it
# resumes (runner/sdk.py keeps runner-session.json; a side session kind
# keeps runner-session-<kind>.json), the generation count (boot.py), and
# the mining and proposal cursors and window (runner/extract.py).
RUNNER_STATE = ("runner-session*.json", "generation.txt",
                "extract-cursor.json", "propose-cursor.json",
                "proposals.json")


class BackupError(Exception):
    pass


class _NoContext(Exception):
    pass


def _home(args):
    home = getattr(args, "home", None) or os.environ.get("COUSIN_HOME")
    if not home:
        raise _NoContext(
            "no cousin context - set COUSIN_HOME or pass --home"
            " (refusing to guess whose home to snapshot)")
    return Path(home).resolve()


def _snapshot_db(src, dst):
    """Consistent copy via VACUUM INTO, staged beside the destination
    and renamed in, so a failed run leaves no half-written database
    and the source directory is never written to."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(str(src), timeout=5)
    try:
        conn.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        conn.close()
    tmp.replace(dst)


def _last_line_end(f):
    """Offset just past the last b"\n" in the open binary file `f`, or 0
    when it holds no complete line. Scans back from the end in blocks,
    so a large stream is never read whole."""
    pos = f.seek(0, 2)
    while pos > 0:
        step = min(STREAM_BLOCK, pos)
        pos -= step
        f.seek(pos)
        i = f.read(step).rfind(b"\n")
        if i >= 0:
            return pos + i + 1
    return 0


def _snapshot_stream(src, dst):
    """Copy one event-stream file, then truncate the COPY to its last
    complete line (the source is never written to). Staged and renamed
    in, as _snapshot_db does."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    shutil.copyfile(src, tmp)
    with open(tmp, "r+b") as f:
        f.truncate(_last_line_end(f))
    tmp.replace(dst)


def _copy_plain(src, dst):
    """A file its writer replaces whole (write a temp, rename it in), so
    a plain copy is never torn."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_memory(src, dst):
    if not src.is_dir():
        return
    shutil.copytree(src, dst, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*MEMORY_SKIP))


def snapshot(home, dest_root):
    """Snapshot <home> into <dest_root>/<slug>/<YYYY-MM-DD>/ and return
    that directory. A second run on the same day overwrites in place."""
    home = Path(home)
    try:
        slug = CousinConfig.load(home).slug
    except MissingConfigError as err:
        raise BackupError(str(err))
    snap = Path(dest_root) / slug / date.today().isoformat()
    snap.mkdir(parents=True, exist_ok=True)
    data = home / "data"
    if data.is_dir():
        inbox = data / INBOX_DB
        # the inbox first, then the rest sorted (the module docstring)
        for db in sorted(data.rglob("*.db"), key=lambda p: (p != inbox, p)):
            try:
                _snapshot_db(db, snap / "data" / db.relative_to(data))
            except sqlite3.Error as err:
                raise BackupError("%s: %s" % (db, err))
        for jsonl in sorted(data.glob(STREAM_GLOB)):
            try:
                _snapshot_stream(jsonl, snap / "data" / jsonl.relative_to(data))
            except OSError as err:
                raise BackupError("%s: %s" % (jsonl, err))
        for state in sorted({p for pattern in RUNNER_STATE
                             for p in data.glob(pattern)}):
            try:
                _copy_plain(state, snap / "data" / state.relative_to(data))
            except OSError as err:
                raise BackupError("%s: %s" % (state, err))
    _copy_memory(home / "memory", snap / "memory")
    for name in CORE_FILES:
        src = home / name
        if src.is_file():
            shutil.copy2(src, snap / name)
    return snap


@traced_cli("cousin-backup")
def backup_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="cousin-backup",
        description="snapshot a cousin home's databases and memory")
    parser.add_argument("--home")
    parser.add_argument("--dest", required=True,
                        help="root directory for snapshots"
                             " (<dest>/<slug>/<date>/)")
    args = parser.parse_args(argv)
    try:
        home = _home(args)
        snap = snapshot(home, Path(args.dest))
    except _NoContext as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2
    except BackupError as err:
        print("ERROR: %s" % err, file=sys.stderr)
        return 2 if "cousin.toml" in str(err) else 1
    print("snapshot written to %s" % snap)
    return 0


if __name__ == "__main__":
    sys.exit(backup_main())
