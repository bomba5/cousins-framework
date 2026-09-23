"""SqliteSessionStore: the SDK's SessionStore protocol over
<home>/data/sessions.db (spec, "The runner": the framework owns the
transcript; phase 0 finding 2: resume reads this store alone).

Entries are opaque JSON the SDK owns; we persist them in append order
and give them back deep-equal. An entry with a `uuid` is idempotent
(a retried batch lands once); one without is appended as is. Every
protocol method runs its SQL on a worker thread: the caller is the
runner's event loop. The SDK is imported inside `append` only (for
fold_session_summary); the module imports without it."""
import asyncio
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from cousin_lib.sqlite_util import wal

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS entries ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " project_key TEXT NOT NULL, session_id TEXT NOT NULL,"
    " subpath TEXT NOT NULL DEFAULT '', uuid TEXT, entry_json TEXT NOT NULL)",
    "CREATE UNIQUE INDEX IF NOT EXISTS entries_uuid"
    " ON entries(project_key, session_id, subpath, uuid) WHERE uuid IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS entries_key ON entries(project_key, session_id, subpath, id)",
    # entries_after and tail_text look a session up without its project key
    "CREATE INDEX IF NOT EXISTS entries_session ON entries(session_id, subpath, id)",
    "CREATE TABLE IF NOT EXISTS keys ("
    " project_key TEXT NOT NULL, session_id TEXT NOT NULL, subpath TEXT NOT NULL DEFAULT '',"
    " mtime INTEGER NOT NULL, PRIMARY KEY (project_key, session_id, subpath))",
    "CREATE TABLE IF NOT EXISTS summaries ("
    " project_key TEXT NOT NULL, session_id TEXT NOT NULL, mtime INTEGER NOT NULL,"
    " data_json TEXT NOT NULL, PRIMARY KEY (project_key, session_id))",
)


class SqliteSessionStore:
    def __init__(self, home):
        self.path = Path(home) / "data" / "sessions.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()     # serializes the read-fold-write of a summary
        self._last_mtime = 0
        with self._db() as conn:
            for stmt in _SCHEMA:
                conn.execute(stmt)

    @contextmanager
    def _db(self):
        """One connection per call, committed on success, always closed
        (the inbox's rule: no connection crosses threads)."""
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            wal(conn)
            with conn:
                yield conn
        finally:
            conn.close()

    def _mtime(self):
        now = int(time.time() * 1000)
        if now <= self._last_mtime:
            now = self._last_mtime + 1
        self._last_mtime = now
        return now

    # -- protocol ----------------------------------------------------------
    async def append(self, key, entries):
        from claude_agent_sdk import fold_session_summary
        await asyncio.to_thread(self._append, key, list(entries), fold_session_summary)

    def _append(self, key, entries, fold):
        pk, sid, sub = key["project_key"], key["session_id"], key.get("subpath") or ""
        with self._lock, self._db() as conn:
            for entry in entries:
                conn.execute("INSERT OR IGNORE INTO entries"
                             " (project_key, session_id, subpath, uuid, entry_json)"
                             " VALUES (?, ?, ?, ?, ?)",
                             (pk, sid, sub, entry.get("uuid"), json.dumps(entry)))
            mtime = self._mtime()
            conn.execute("INSERT INTO keys VALUES (?, ?, ?, ?) ON CONFLICT"
                         " (project_key, session_id, subpath) DO UPDATE SET mtime = excluded.mtime",
                         (pk, sid, sub, mtime))
            if not sub:
                row = conn.execute("SELECT mtime, data_json FROM summaries"
                                   " WHERE project_key = ? AND session_id = ?", (pk, sid)).fetchone()
                prev = ({"session_id": sid, "mtime": row[0], "data": json.loads(row[1])}
                        if row else None)
                folded = fold(prev, key, entries)
                conn.execute("INSERT INTO summaries VALUES (?, ?, ?, ?) ON CONFLICT"
                             " (project_key, session_id) DO UPDATE SET mtime = excluded.mtime,"
                             " data_json = excluded.data_json",
                             (pk, sid, mtime, json.dumps(folded["data"])))

    async def load(self, key):
        return await asyncio.to_thread(self._load, key)

    def _load(self, key):
        pk, sid, sub = key["project_key"], key["session_id"], key.get("subpath") or ""
        with self._db() as conn:
            known = conn.execute("SELECT 1 FROM keys WHERE project_key = ? AND session_id = ?"
                                 " AND subpath = ?", (pk, sid, sub)).fetchone()
            if not known:
                return None
            rows = conn.execute("SELECT entry_json FROM entries WHERE project_key = ?"
                                " AND session_id = ? AND subpath = ? ORDER BY id",
                                (pk, sid, sub)).fetchall()
        return [json.loads(r[0]) for r in rows]

    async def list_sessions(self, project_key):
        def run():
            with self._db() as conn:
                rows = conn.execute("SELECT session_id, mtime FROM keys WHERE project_key = ?"
                                    " AND subpath = ''", (project_key,)).fetchall()
            return [{"session_id": sid, "mtime": mtime} for sid, mtime in rows]
        return await asyncio.to_thread(run)

    async def list_session_summaries(self, project_key):
        def run():
            with self._db() as conn:
                rows = conn.execute("SELECT session_id, mtime, data_json FROM summaries"
                                    " WHERE project_key = ?", (project_key,)).fetchall()
            return [{"session_id": sid, "mtime": mtime, "data": json.loads(data)}
                    for sid, mtime, data in rows]
        return await asyncio.to_thread(run)

    async def delete(self, key):
        def run():
            pk, sid, sub = key["project_key"], key["session_id"], key.get("subpath")
            with self._lock, self._db() as conn:
                if sub:     # a targeted delete removes that one subkey
                    for table in ("entries", "keys"):
                        conn.execute("DELETE FROM %s WHERE project_key = ? AND session_id = ?"
                                     " AND subpath = ?" % table, (pk, sid, sub))
                    return
                for table in ("entries", "keys", "summaries"):   # the main key cascades
                    conn.execute("DELETE FROM %s WHERE project_key = ? AND session_id = ?"
                                 % table, (pk, sid))
        await asyncio.to_thread(run)

    async def list_subkeys(self, key):
        def run():
            with self._db() as conn:
                rows = conn.execute("SELECT subpath FROM keys WHERE project_key = ?"
                                    " AND session_id = ? AND subpath != '' ORDER BY subpath",
                                    (key["project_key"], key["session_id"])).fetchall()
            return [r[0] for r in rows]
        return await asyncio.to_thread(run)

    # -- the framework's own reads (synchronous; never on the loop) ------------
    def entries_after(self, session_id, cursor=0):
        with self._db() as conn:
            rows = conn.execute("SELECT id, entry_json FROM entries WHERE session_id = ?"
                                " AND subpath = '' AND id > ? ORDER BY id",
                                (session_id, int(cursor))).fetchall()
        if not rows:
            return [], int(cursor)
        return [json.loads(r[1]) for r in rows], rows[-1][0]

    TAIL_ENTRIES = 200

    def tail_text(self, session_id, max_chars=2000):
        """A BOUNDED tail: the last TAIL_ENTRIES main-transcript entries,
        never the whole session (an emergency handoff reads this at the end
        of a long generation)."""
        with self._db() as conn:
            rows = conn.execute("SELECT entry_json FROM entries WHERE session_id = ?"
                                " AND subpath = '' ORDER BY id DESC LIMIT ?",
                                (session_id, self.TAIL_ENTRIES)).fetchall()
        entries = [json.loads(r[0]) for r in reversed(rows)]
        texts = []
        for e in entries:
            if e.get("type") != "assistant":
                continue
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list):
                texts += [b.get("text", "") for b in content
                          if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        text = "\n".join(texts)
        return text[-max_chars:] if len(text) > max_chars else text
