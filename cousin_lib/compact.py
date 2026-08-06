"""Memory-index compaction: pointer hygiene, never deletion.

MEMORY.md is one pointer line per topic file and loads every session,
so it must stay bounded - but compacting it is HYGIENE: a pointer may
be retired only when its topic file is independently reachable, on
disk AND in the search index, so retiring the line loses nothing.

The invariants, each earned in the source framework:

- Uncertainty keeps. An unparsable date is a keep, not a candidate -
  a retention transform and a privacy gate default in opposite
  directions, and this is the retention one.
- The hot window (last N days) and anything pinned never move.
- Size-targeted, oldest-first, STOP at budget - never sweep.
- A timestamped snapshot precedes every run and old snapshots are
  pruned; crash anywhere means the snapshot restores and a re-run is
  safe.
- Single atomic rename: readers must never see a half-written index.

The source framework had a second transform (an append-only timeline
whose old entries relocate to an archive); the public layout has no
timeline convention, so that transform stays out until something
defines one.
"""
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

PIN_MARKER = "[pin]"
SNAPSHOT_KEEP_DAYS = 14
DEFAULT_BUDGET = 24_000
DEFAULT_HOT_DAYS = 7

_POINTER_RE = re.compile(
    r"^\s*-\s*\[(?P<title>[^\]]+)\]\((?P<file>[^)]+)\)(?P<rest>.*)$")
_DATE_RE = re.compile(r"(20\d{2}-\d{2}-\d{2})")


def _parse_pointer(line):
    m = _POINTER_RE.match(line)
    if not m:
        return None
    d = _DATE_RE.search(m.group("rest"))
    return {"file": m.group("file"),
            "date": d.group(1) if d else None,
            "pinned": PIN_MARKER in line}


def _in_search_index(home, fname):
    """Reachability leg two: the topic file must be findable by search,
    or retiring its pointer hides it from everything but ls."""
    db = home / "memory" / "fts_index.db"
    if not db.exists():
        return False
    con = sqlite3.connect(db)
    try:
        row = con.execute(
            "SELECT 1 FROM memory_fts WHERE path LIKE ? LIMIT 1",
            ("%/" + fname,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False
    finally:
        con.close()


def _snapshot(home, index_path):
    snap_dir = home / "memory" / ".compact-snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    shutil.copy2(index_path, snap_dir / ("MEMORY-%s.md" % stamp))
    cutoff = time.time() - SNAPSHOT_KEEP_DAYS * 86400
    for old in snap_dir.glob("MEMORY-*.md"):
        if old.stat().st_mtime < cutoff:
            old.unlink()


def compact_index(home, *, budget=DEFAULT_BUDGET,
                  hot_days=DEFAULT_HOT_DAYS, dry_run=False):
    """Retire the oldest retirable pointers until the index fits the
    byte budget. Returns a report; with dry_run the file is untouched
    and the report says what WOULD retire."""
    home = Path(home)
    index_path = home / "MEMORY.md"
    if not index_path.exists():
        return {"ok": False, "reason": "no MEMORY.md"}
    lines = index_path.read_text().splitlines(keepends=True)
    size = sum(len(l) for l in lines)
    report = {"ok": True, "size_before": size, "retired": [],
              "would_retire": []}
    if size <= budget:
        return report
    hot_cutoff = (datetime.now() - timedelta(days=hot_days)) \
        .strftime("%Y-%m-%d")
    candidates = []
    for i, line in enumerate(lines):
        pointer = _parse_pointer(line)
        if pointer is None or pointer["pinned"]:
            continue
        if pointer["date"] is None:
            continue  # uncertainty keeps
        if pointer["date"] >= hot_cutoff:
            continue
        fname = pointer["file"]
        if not (home / "memory" / fname).exists():
            continue  # unreachable on disk: retiring orphans it
        if not _in_search_index(home, fname):
            continue  # unreachable by search: same
        candidates.append((pointer["date"], i, fname))
    candidates.sort()  # oldest first
    retire = set()
    for date, i, fname in candidates:
        if size <= budget:
            break  # STOP at budget, never sweep
        size -= len(lines[i])
        retire.add(i)
        report["would_retire"].append(fname)
    if dry_run or not retire:
        return report
    _snapshot(home, index_path)
    kept = [l for i, l in enumerate(lines) if i not in retire]
    tmp = index_path.with_suffix(".md.tmp")
    tmp.write_text("".join(kept))
    os.replace(tmp, index_path)
    report["retired"] = report["would_retire"]
    report["size_after"] = sum(len(l) for l in kept)
    return report
