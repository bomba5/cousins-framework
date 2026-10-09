"""Expected-action audits.

Framework checks - not cousin self-discipline - that the session-end
rituals happened: status updated, handoff written, open threads marked,
durable memory extracted. Detection is file mtimes against the session
start; violations land in an audit database that correction machinery
and operators can read.

The one rule that shapes the checks: an audit may only demand actions
that shipped tools actually produce. Demanding a specific artifact
with no producer files a violation on every session for every cousin -
pure noise that trains everyone to ignore the audit.
"""
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from cousin_lib import atomic
from cousin_lib import status_sections
from cousin_lib.config import FrameworkConfig


def _connect():
    path = FrameworkConfig.from_env().root / "data" / "audit-violations.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=2.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS audit_violation ("
        " id           INTEGER PRIMARY KEY AUTOINCREMENT,"
        " ts           INTEGER NOT NULL,"
        " cousin       TEXT NOT NULL,"
        " generation   INTEGER,"
        " category     TEXT NOT NULL,"
        " action       TEXT NOT NULL,"
        " detail       TEXT,"
        " corrected_at INTEGER)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_cousin_ts"
        " ON audit_violation(cousin, ts)"
    )
    return con


def _mtime(path):
    try:
        return Path(path).stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _durable_memory_mtime(home):
    """Latest mtime across every surface where a cousin writes durable
    memory: raw candidates, the decisions log, memory/*.md. Index
    artifacts are deliberately not counted - reindex churn is not
    memory extraction."""
    latest = 0.0
    raw = home / "memory" / "raw"
    if raw.is_dir():
        for p in raw.glob("*.jsonl"):
            latest = max(latest, _mtime(p))
    latest = max(latest, _mtime(home / "data" / "decisions.jsonl"))
    mem = home / "memory"
    if mem.is_dir():
        for p in mem.glob("*.md"):
            latest = max(latest, _mtime(p))
    return latest


def audit_before_exit(slug, home, *, since_ts):
    """Check the session-end actions; anything with mtime newer than
    the session start counts as performed. Violations are recorded and
    returned."""
    home = Path(home)
    expected = {
        "update_status": home / "STATUS.md",
        "write_handoff": home / "data" / "handoff.md",
        "mark_open_threads": home / "data" / "active-threads.md",
    }
    violations = []
    for action, path in expected.items():
        latest = _mtime(path)
        if latest <= since_ts:
            violations.append({
                "action": action, "path": str(path),
                "last_mtime": latest, "since_ts": since_ts,
            })
    latest = _durable_memory_mtime(home)
    if latest <= since_ts:
        violations.append({
            "action": "extract_durable_memories",
            "path": str(home / "memory"),
            "last_mtime": latest, "since_ts": since_ts,
        })
    if violations:
        record_violations(slug, "before_exit", violations)
    return violations


def record_violations(cousin, category, violations, *, generation=0):
    if not violations:
        return 0
    con = _connect()
    try:
        now = int(time.time())
        for v in violations:
            con.execute(
                "INSERT INTO audit_violation"
                " (ts, cousin, generation, category, action, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (now, cousin, generation, category,
                 v.get("action", "?"), json.dumps(v, default=str)[:500]),
            )
        con.commit()
        return len(violations)
    finally:
        con.close()


def list_violations(cousin, *, since_hours=24):
    cutoff = int(time.time()) - since_hours * 3600
    con = _connect()
    try:
        rows = con.execute(
            "SELECT id, ts, cousin, generation, category, action,"
            " detail, corrected_at FROM audit_violation"
            " WHERE cousin=? AND ts>=? ORDER BY ts DESC",
            (cousin, cutoff),
        ).fetchall()
    finally:
        con.close()
    cols = ["id", "ts", "cousin", "generation", "category", "action",
            "detail", "corrected_at"]
    return [dict(zip(cols, r)) for r in rows]


def mark_corrected(violation_id):
    con = _connect()
    try:
        con.execute(
            "UPDATE audit_violation SET corrected_at=? WHERE id=?",
            (int(time.time()), violation_id),
        )
        con.commit()
    finally:
        con.close()


# The baseline fallback: when a session ends without hand-curated open
# threads, derive a baseline from STATUS.md so the next session starts
# from something rather than nothing.

_BASELINE_MARK = (
    "<!-- baseline derived from STATUS open-loops at session-end -->"
)


def _extract_open_loops(status_text):
    """The body of STATUS.md's live open-loops section, as a one-item list
    ([] when it is missing or empty): the section the handoff writes, read
    with the one shared definition (cousin_lib.status_sections). A
    suffixed '## Open loops (...)' heading is history, not a loop."""
    body = "\n".join(status_sections.open_loops_body(status_text).splitlines())
    body = body.strip("\n").rstrip()
    return [body] if body.strip() else []


def derive_active_threads_baseline(home):
    """Baseline data/active-threads.md body from STATUS.md, marked so a
    reader can tell auto-derived from hand-curated. Empty string when
    STATUS.md is missing or its live Open-loops section is empty."""
    try:
        status_text = (Path(home) / "STATUS.md").read_text()
    except FileNotFoundError:
        return ""
    blocks = _extract_open_loops(status_text)
    if not blocks:
        return ""
    now = datetime.now().replace(microsecond=0).isoformat()
    parts = [
        "# Active threads (auto-derived baseline)",
        "_Last auto-derived: %s_" % now,
        "",
        _BASELINE_MARK,
        "",
        "Synced from STATUS.md's '## Open loops' section at session-end."
        " Hand-curate this file during the session to replace the"
        " baseline; do not delete this header.",
        "",
        "## Open loops (verbatim from STATUS.md)",
        "",
    ]
    parts.extend(blocks)
    return "\n".join(parts).rstrip() + "\n"


def write_active_threads_baseline(home, *, since_ts=0.0, force=False):
    """Write the baseline if the file is missing or predates the
    session. A file the cousin wrote THIS session is never overwritten:
    hand-curated content beats a derived baseline."""
    path = Path(home) / "data" / "active-threads.md"
    cur_mtime = _mtime(path)
    fresh = cur_mtime > since_ts
    if fresh and not force:
        return {"wrote": False, "reason": "fresh-skip",
                "path": str(path), "chars": 0}
    body = derive_active_threads_baseline(home)
    if not body:
        return {"wrote": False, "reason": "no-open-loops",
                "path": str(path), "chars": 0}
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(path, body)
    if force and fresh:
        reason = "forced"
    elif cur_mtime == 0.0:
        reason = "wrote-new"
    else:
        reason = "wrote-stale"
    return {"wrote": True, "reason": reason,
            "path": str(path), "chars": len(body)}
