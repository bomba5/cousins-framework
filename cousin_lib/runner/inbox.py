"""The durable inbox: the store is the bus.

docs/reference/runners.md, "What a runner is". A producer
inserts a row here and pokes the runner; nothing needs to be running
for the insert to succeed, and a row survives any process dying
between put and claim.

A turn is NOT 1:1 with a row: a message folded
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


# A thread id's kind in SQL: the text before the first colon, or the whole
# id for a bare kind ("schedule", "system"); delivery.parse_thread's rule.
_KIND_SQL = ("CASE WHEN instr(thread_id, ':') > 0"
             " THEN substr(thread_id, 1, instr(thread_id, ':') - 1) ELSE thread_id END")


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
            # a producer's idempotency key (delivery.keyed): one row per key
            add_columns(conn, "inbox", {"dedup_key": "TEXT"})
            conn.execute("CREATE INDEX IF NOT EXISTS inbox_claim"
                         " ON inbox(state, priority, id)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS inbox_dedup"
                         " ON inbox(dedup_key) WHERE dedup_key IS NOT NULL")

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

    def put(self, item, *, rank=None, key=None):
        """Queue `item`; its priority is base.priority's for its source and
        thread unless `rank` is given (a lower rank is claimed first: the
        kind switch's notice goes ahead of every row queued before it).
        With a `key` (delivery.keyed) a row already put under it is
        returned instead, whatever its state: nothing new is queued."""
        if not isinstance(item, Item):
            raise TypeError("put() takes a delivery.Item")
        with self._db() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO inbox (thread_id, source, sender, body,"
                    " attachments_json, context, message_id, priority, state,"
                    " created_at, dedup_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (item.thread_id, item.source, item.sender, item.body,
                     json.dumps(list(item.attachments)), item.context,
                     item.message_id,
                     priority(item.source, item.thread_id) if rank is None else int(rank),
                     QUEUED, time.time(), key))
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT id FROM inbox WHERE dedup_key=?", (key,)).fetchone()
                if key is None or row is None:
                    raise
                return row[0]
            return cur.lastrowid

    def keyed_id(self, key):
        """The id of the row put under `key`, or None."""
        with self._db() as conn:
            row = conn.execute("SELECT id FROM inbox WHERE dedup_key=?", (key,)).fetchone()
        return row[0] if row else None

    def claim(self, *, limit=1, claimant="", kinds=None, exclude_kinds=()):
        """Oldest first within priority. BEGIN IMMEDIATE takes the write
        lock before the select, so two runners (or a runner and a test)
        never claim the same row. `kinds`: only rows whose
        thread is of one of these kinds, `()` claiming nothing;
        `exclude_kinds`: never a row of these kinds. A side session claims
        its kinds, the primary everything but them."""
        claimant = claimant or "pid:%d" % os.getpid()
        where, params = "state=?", [QUEUED]
        if kinds is not None:
            kinds = tuple(kinds)
            if not kinds:
                return []
            where += " AND %s IN (%s)" % (_KIND_SQL, ",".join("?" * len(kinds)))
            params += kinds
        if exclude_kinds:
            exclude_kinds = tuple(exclude_kinds)
            where += " AND %s NOT IN (%s)" % (_KIND_SQL, ",".join("?" * len(exclude_kinds)))
            params += exclude_kinds
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                ids = [r[0] for r in conn.execute(
                    "SELECT id FROM inbox WHERE %s ORDER BY priority, id"
                    " LIMIT ?" % where, (*params, limit))]
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

    def claim_id(self, inbox_id, *, claimant=""):
        """Claim THIS row if it is still queued; None otherwise. For a
        runner that must run a known row next (the rollover's digest),
        whatever its priority relative to the queue."""
        with self._db() as conn:
            cur = conn.execute("UPDATE inbox SET state=?, claimant=?, claimed_at=?"
                               " WHERE id=? AND state=?",
                               (CLAIMED, claimant, time.time(), inbox_id, QUEUED))
            if cur.rowcount != 1:
                return None
        return self.get(inbox_id)

    def replace_body(self, inbox_id, body):
        """Replace a QUEUED row's body; False when it was claimed meanwhile."""
        with self._db() as conn:
            cur = conn.execute("UPDATE inbox SET body=? WHERE id=? AND state=?",
                               (body, inbox_id, QUEUED))
            return cur.rowcount == 1

    def open_rows(self, source):
        """The queued or claimed rows of one source, oldest first."""
        with self._db() as conn:
            rows = conn.execute("SELECT %s FROM inbox WHERE source=? AND state IN (?, ?)"
                                " ORDER BY id" % ", ".join(_COLUMNS),
                                (source, QUEUED, CLAIMED)).fetchall()
        return [_row(r) for r in rows]

    def done(self, inbox_id, outcome, detail=""):
        with self._db() as conn:
            conn.execute(
                "UPDATE inbox SET state=?, outcome=?, detail=?, done_at=?"
                " WHERE id=?", (DONE, outcome, detail, time.time(), inbox_id))

    def done_if_queued(self, inbox_id, outcome, detail="", *, body):
        """Close a row only while it is still queued with exactly `body`;
        False otherwise. A close decided on a snapshot (a duplicate
        rollover row) must not land on a row claimed or rewritten since."""
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE inbox SET state=?, outcome=?, detail=?, done_at=?"
                " WHERE id=? AND state=? AND body=?",
                (DONE, outcome, detail, time.time(), inbox_id, QUEUED, body))
            return cur.rowcount == 1

    def done_if_open(self, inbox_id, outcome, detail=""):
        """Close a row that is queued or claimed (never one already done);
        False otherwise. A claimed row closed here is out of requeue_stale's
        reach, so no later start runs it."""
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE inbox SET state=?, outcome=?, detail=?, done_at=?"
                " WHERE id=? AND state IN (?, ?)",
                (DONE, outcome, detail, time.time(), inbox_id, QUEUED, CLAIMED))
            return cur.rowcount == 1

    def requeue(self, inbox_id):
        """Return one row to queued, clearing claim and outcome. Idempotent:
        a missing id is a no-op. Used to undo a claim without going through
        `done`, e.g. when a runner re-dispatches a row into a fresh turn."""
        with self._db() as conn:
            conn.execute(
                "UPDATE inbox SET state=?, outcome=NULL, claimant='',"
                " claimed_at=NULL, done_at=NULL WHERE id=?",
                (QUEUED, inbox_id))

    def requeue_claimant(self, claimant):
        """Every row still claimed by `claimant` back to queued (a
        side session that gave up). Returns how many."""
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE inbox SET state=?, claimant='', claimed_at=NULL"
                " WHERE state=? AND claimant=?", (QUEUED, CLAIMED, claimant))
            return cur.rowcount

    def close_recorded(self):
        """Close every claimed row whose claimant's stream already holds the
        result that names it: a runner records a turn's result, then closes
        its rows, and one killed in between left them claimed. Requeued,
        they would run their turn again. Only for a runner whose claimant is
        its stream's name, before `requeue_stale`, with no runner alive on
        the home. Returns the ids closed."""
        from cousin_lib.delivery import DELIVERED, FAILED
        from cousin_lib.runner import stream
        with self._db() as conn:
            claimed = conn.execute("SELECT id, claimant FROM inbox WHERE state=?",
                                   (CLAIMED,)).fetchall()
        by_claimant = {}
        for inbox_id, claimant in claimed:
            if claimant:
                by_claimant.setdefault(claimant, []).append(inbox_id)
        closed = []
        for claimant, ids in sorted(by_claimant.items()):
            if "/" in claimant or claimant.startswith("."):
                continue
            found = stream.results_naming(stream.path_for(self.home, claimant), ids)
            for inbox_id in ids:
                event = found.get(inbox_id)
                if event is None:
                    continue
                p = event.get("payload") or {}
                outcome = FAILED if p.get("is_error") and not p.get("interrupted") else DELIVERED
                self.done(inbox_id, outcome, "closed at restart: the turn's result was recorded"
                          " (stream %s seq %s)" % (claimant, event.get("seq")))
                closed.append(inbox_id)
        return closed

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
