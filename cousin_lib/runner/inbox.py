"""The durable inbox: the store is the bus.

docs/design/agent-loop-runner.md, "The store is the bus". A producer
inserts a row here and pokes the runner; nothing needs to be running
for the insert to succeed, and a row survives any process dying
between put and claim.

A turn is NOT 1:1 with a row (phase 0 finding 1): a message folded
into a running turn is closed by that turn's one result, so the runner
calls `done` for every row the turn consumed.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cousin_lib.delivery import Item
from cousin_lib.runner.base import priority
from cousin_lib.sqlite_util import add_columns, wal

QUEUED, CLAIMED, DONE = "queued", "claimed", "done"

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS inbox ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " thread_id TEXT NOT NULL,"
    " source TEXT NOT NULL,"
    " sender TEXT NOT NULL DEFAULT '',"
    " body TEXT NOT NULL,"
    " attachments_json TEXT NOT NULL DEFAULT '[]',"
    " context TEXT NOT NULL DEFAULT '',"
    " message_id INTEGER,"
    " priority INTEGER NOT NULL DEFAULT 3,"
    " state TEXT NOT NULL DEFAULT 'queued',"
    " outcome TEXT,"
    " detail TEXT NOT NULL DEFAULT '',"
    " claimant TEXT NOT NULL DEFAULT '',"
    " created_at REAL NOT NULL,"
    " claimed_at REAL,"
    " done_at REAL)"
)
_COLUMNS = ["id", "thread_id", "source", "sender", "body", "attachments_json",
            "context", "message_id", "priority", "state", "outcome", "detail",
            "claimant", "created_at", "claimed_at", "done_at"]


def _row(cur_row):
    row = dict(zip(_COLUMNS, cur_row))
    row["attachments"] = json.loads(row.pop("attachments_json") or "[]")
    return row


class Inbox:
    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / "data" / "inbox.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.execute(_SCHEMA)
            add_columns(conn, "inbox", {})  # additive columns land here later
            conn.execute("CREATE INDEX IF NOT EXISTS inbox_claim"
                         " ON inbox(state, priority, id)")

    @contextmanager
    def _db(self):
        """Every call opens its own connection and closes it on the way
        out (the codebase's `_db()` convention, cousin_lib/jobs.py): an
        `sqlite3.Connection` used bare as `with conn:` only guards the
        transaction, it never closes the fd, and a suite that opens one
        per call would otherwise leak one per call."""
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        wal(conn)
        try:
            yield conn
        finally:
            conn.close()

    def put(self, item):
        if not isinstance(item, Item):
            raise TypeError("put() takes a delivery.Item")
        with self._db() as conn:
            cur = conn.execute(
                "INSERT INTO inbox (thread_id, source, sender, body,"
                " attachments_json, context, message_id, priority, state,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item.thread_id, item.source, item.sender, item.body,
                 json.dumps(list(item.attachments)), item.context,
                 item.message_id, priority(item.source, item.thread_id),
                 QUEUED, time.time()))
            return cur.lastrowid

    def claim(self, *, limit=1, claimant=""):
        """Oldest first within priority. BEGIN IMMEDIATE takes the write
        lock before the select, so two runners (or a runner and a test)
        never claim the same row."""
        claimant = claimant or "pid:%d" % os.getpid()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM inbox WHERE state=? ORDER BY priority, id"
                    " LIMIT ?", (QUEUED, limit))]
                if not ids:
                    conn.execute("COMMIT")
                    return []
                marks = ",".join("?" * len(ids))
                conn.execute(
                    "UPDATE inbox SET state=?, claimant=?, claimed_at=?"
                    " WHERE id IN (%s)" % marks,
                    [CLAIMED, claimant, time.time(), *ids])
                rows = conn.execute(
                    "SELECT %s FROM inbox WHERE id IN (%s) ORDER BY priority, id"
                    % (",".join(_COLUMNS), marks), ids).fetchall()
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return [_row(r) for r in rows]

    def done(self, inbox_id, outcome, detail=""):
        with self._db() as conn:
            conn.execute(
                "UPDATE inbox SET state=?, outcome=?, detail=?, done_at=?"
                " WHERE id=?", (DONE, outcome, detail, time.time(), inbox_id))

    def requeue(self, inbox_id):
        """Return one row to queued, clearing claim and outcome. Idempotent:
        a missing id is a no-op. Used to undo a claim without going through
        `done`, e.g. when a runner re-dispatches a row into a fresh turn."""
        with self._db() as conn:
            conn.execute(
                "UPDATE inbox SET state=?, outcome=NULL, claimant='',"
                " claimed_at=NULL, done_at=NULL WHERE id=?",
                (QUEUED, inbox_id))

    def requeue_stale(self, older_than_s):
        cutoff = time.time() - float(older_than_s)
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE inbox SET state=?, claimant='', claimed_at=NULL"
                " WHERE state=? AND claimed_at IS NOT NULL AND claimed_at <= ?",
                (QUEUED, CLAIMED, cutoff))
            return cur.rowcount

    def pending(self):
        with self._db() as conn:
            return conn.execute("SELECT COUNT(*) FROM inbox WHERE state=?",
                                (QUEUED,)).fetchone()[0]

    def unfinished(self):
        """Count of rows not yet done: queued or claimed."""
        with self._db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE state IN (?,?)",
                (QUEUED, CLAIMED)).fetchone()[0]

    def get(self, inbox_id):
        with self._db() as conn:
            r = conn.execute("SELECT %s FROM inbox WHERE id=?"
                             % ",".join(_COLUMNS), (inbox_id,)).fetchone()
        return _row(r) if r else None
