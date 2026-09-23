"""Tool-trace ledger: what a cousin's CLI surface was asked to do.

A cousin does not need to remember which tools it called - the ledger
does, and the boot packet's trace layer replays the recent tail so a
fresh session inherits its predecessor's working context.

Two postures, both deliberate:

- Logging is BEST-EFFORT: a tracing hiccup must never break the call
  it was recording, so failures return None and raise nothing. For
  the caller, a failed trace write is a genuine no-op - which is what
  makes this silence legitimate under the absence rule.
- Reading is honest: an empty ledger renders the idle marker, which
  the boot assembler's degraded rules already treat as legitimate.
"""
import functools
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from cousin_lib.config import CousinConfig, FrameworkConfig


def _connect(root=None):
    base = Path(root) if root is not None else FrameworkConfig.from_env().root
    path = base / "data" / "trace-ledger.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=2.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS trace_ledger ("
        " id             INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts             INTEGER NOT NULL,"
        " cousin         TEXT NOT NULL,"
        " tool           TEXT NOT NULL,"
        " args_summary   TEXT,"
        " result_summary TEXT)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_trace_cousin_ts"
        " ON trace_ledger(cousin, ts)"
    )
    return con


def log_call(cousin, tool, *, args_summary=None, result_summary=None):
    """Append one trace row; returns the id, or None on any failure."""
    try:
        if isinstance(args_summary, dict):
            args_summary = json.dumps(args_summary, default=str)
        con = _connect()
        try:
            cur = con.execute(
                "INSERT INTO trace_ledger"
                " (ts, cousin, tool, args_summary, result_summary)"
                " VALUES (?, ?, ?, ?, ?)",
                (int(time.time()), cousin, tool,
                 (args_summary or "")[:500],
                 (result_summary or "")[:300]),
            )
            con.commit()
            return cur.lastrowid
        finally:
            con.close()
    except Exception:
        return None


def recent_calls(cousin, *, n=30, since_hours=24, root=None):
    cutoff = int(time.time()) - since_hours * 3600
    try:
        con = _connect(root)
    except Exception:
        return []
    try:
        rows = con.execute(
            "SELECT ts, tool, args_summary, result_summary"
            " FROM trace_ledger WHERE cousin=? AND ts>=?"
            " ORDER BY id DESC LIMIT ?",
            (cousin, cutoff, n),
        ).fetchall()
    finally:
        con.close()
    return [
        {"ts": ts, "tool": tool, "args_summary": args or "",
         "result_summary": result or ""}
        for ts, tool, args, result in rows
    ]


def summary_for_boot(cousin, *, n=30, since_hours=24, root=None):
    """The boot layer's view: newest first, one line per call, or the
    idle marker the degraded rules already know is legitimate."""
    rows = recent_calls(cousin, n=n, since_hours=since_hours, root=root)
    if not rows:
        return "(no substantive tool traces in last %dh)" % since_hours
    lines = []
    for row in rows:
        stamp = datetime.fromtimestamp(row["ts"]).strftime("%H:%M")
        line = "- [%s] %s" % (stamp, row["tool"])
        if row["args_summary"]:
            line += " " + row["args_summary"]
        if row["result_summary"]:
            line += " -> " + row["result_summary"]
        lines.append(line)
    return "\n".join(lines)


def traced_cli(tool):
    """Wrap a CLI main so every invocation lands in the ledger. The
    slug comes from COUSIN_HOME; a contextless invocation skips
    tracing silently - tracing without an identity would attribute
    the call to nobody, and the CLI must keep working either way."""

    def wrap(fn):
        @functools.wraps(fn)
        def inner(argv=None):
            rc = fn(argv)
            try:
                slug = CousinConfig.from_env().slug
                shown = argv if argv is not None else sys.argv[1:]
                log_call(slug, tool,
                         args_summary=" ".join(shown)[:200],
                         result_summary="rc=%s" % (rc or 0))
            except Exception:
                pass
            return rc
        return inner

    return wrap
