"""Backup snapshots of a cousin home: databases via VACUUM INTO,
memory and the core markdown files as plain copies.

VACUUM INTO gives a consistent copy of a database another process
holds open in WAL mode; a file copy of the same database can be torn.
Rebuildable search indexes under memory/ are not memory and are not
snapshotted - a restore regenerates them on the first search.

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
        for db in sorted(data.rglob("*.db")):
            try:
                _snapshot_db(db, snap / "data" / db.relative_to(data))
            except sqlite3.Error as err:
                raise BackupError("%s: %s" % (db, err))
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
