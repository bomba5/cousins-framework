"""Keyword search over a cousin's memory and notes.

The v1 tier: SQLite FTS5 with BM25 ranking over <home>/memory/*.md and
<home>/notes/*.md. Semantic search is the declared M2 seam - it adds
an optional external model service, and keyword search is the path
that keeps day-one true. Nothing here pretends to be semantic.

Context comes from COUSIN_HOME and fails loud: a search that silently
reads nothing teaches its caller that memory is empty, which is worse
than an error.
"""
import os
import re
import sqlite3
import time
from pathlib import Path

from cousin_lib.config import MissingConfigError


def _home():
    home = os.environ.get("COUSIN_HOME")
    if not home:
        raise MissingConfigError(
            "COUSIN_HOME is not set; refusing to search without a"
            " cousin context"
        )
    return Path(home)


def _fts_path(home):
    return home / "memory" / "fts_index.db"


def _sources(home):
    """(collection, path) pairs for every indexable file. The memory
    and notes directories are the cousin's whole searchable surface in
    v1; index artifacts are excluded by suffix."""
    out = []
    for collection, sub in (("memory", "memory"), ("notes", "notes")):
        base = home / sub
        if base.is_dir():
            for path in sorted(base.rglob("*.md")):
                out.append((collection, path))
    return out


def build_index(home=None):
    """(Re)build the FTS index over the current sources. Returns the
    number of files indexed."""
    home = Path(home) if home else _home()
    db_path = _fts_path(home)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DROP TABLE IF EXISTS memory_fts")
        conn.execute(
            "CREATE VIRTUAL TABLE memory_fts USING fts5("
            " collection, path, body)"
        )
        count = 0
        latest = 0.0
        for collection, path in _sources(home):
            try:
                body = path.read_text(errors="replace")
            except OSError:
                continue
            conn.execute(
                "INSERT INTO memory_fts (collection, path, body)"
                " VALUES (?, ?, ?)",
                (collection, str(path), body),
            )
            latest = max(latest, path.stat().st_mtime)
            count += 1
        conn.execute("DROP TABLE IF EXISTS index_meta")
        conn.execute("CREATE TABLE index_meta (built_at REAL,"
                     " source_mtime REAL, files INTEGER)")
        conn.execute("INSERT INTO index_meta VALUES (?, ?, ?)",
                     (time.time(), latest, count))
        conn.commit()
        return count
    finally:
        conn.close()


def _index_stale(home):
    """The index is stale when any source is newer than the newest
    source at build time, or when the file counts differ (a deleted
    file also invalidates)."""
    db_path = _fts_path(home)
    if not db_path.exists():
        return True
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT source_mtime, files FROM index_meta").fetchone()
    except sqlite3.OperationalError:
        return True
    finally:
        conn.close()
    if not row:
        return True
    built_mtime, built_files = row
    sources = _sources(home)
    if len(sources) != built_files:
        return True
    return any(p.stat().st_mtime > built_mtime for _, p in sources)


def _sanitize(query, max_tokens=32):
    """Make a natural-language query safe for FTS5 MATCH. FTS5 treats
    -, /, ', ( as query syntax; a raw cousin question used to error,
    and the keyword leg swallowed it silently - a search that never
    errors and never matches is the worst version. Reduce to quoted
    alphanumeric tokens OR-joined."""
    tokens = re.findall(r"[A-Za-z0-9_]+", query)[:max_tokens]
    if not tokens:
        return ""
    return " OR ".join('"%s"' % t for t in tokens)


def search(query, *, top=5, home=None):
    """Ranked keyword hits: [{path, collection, score, snippet}]. The
    index self-heals on staleness so a fresh file is findable without
    an explicit reindex."""
    home = Path(home) if home else _home()
    if _index_stale(home):
        build_index(home)
    match = _sanitize(query)
    if not match:
        return []
    conn = sqlite3.connect(_fts_path(home))
    try:
        rows = conn.execute(
            "SELECT collection, path, bm25(memory_fts) AS score,"
            " snippet(memory_fts, 2, '[', ']', '...', 12)"
            " FROM memory_fts WHERE memory_fts MATCH ?"
            " ORDER BY score LIMIT ?",
            (match, top),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    return [
        {"collection": c, "path": p, "score": s, "snippet": snip}
        for c, p, s, snip in rows
    ]


def print_results(hits):
    if not hits:
        print("no matches")
        return
    for i, hit in enumerate(hits, 1):
        print("%d. [%.3f] [%s] %s" % (i, hit["score"],
                                      hit["collection"], hit["path"]))
        print("   %s" % hit["snippet"].replace("\n", " "))
