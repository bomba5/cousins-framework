"""Chat message storage for a cousin (what server/chat_api.py reads and
writes in-process).

One SQLite database per cousin at <home>/data/chat.db. The full schema is
declared at creation - there is no migration dance and no dual id space.
WAL mode keeps readers unblocked during writes; the small autocheckpoint
keeps the WAL from growing unbounded across restarts.
"""
import base64
import binascii
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from cousin_lib.sqlite_util import add_column


def normalize_chat_user(name):
    """Thread key for a display name: case- and whitespace-insensitive so
    'Sam Vimes' and 'sam vimes' land in one thread. Empty means the caller
    sent no usable name; those rows still need a queryable key."""
    if not name:
        return "unknown"
    # NFC first: "Totò" typed composed or decomposed is one person, one thread
    return unicodedata.normalize("NFC", name).lower().replace(" ", "_")


def is_operator(config, user):
    """Is this sender the configured operator? No operator configured
    means nobody is: the null profile is "no operator", never a
    defaulted human being."""
    operator = getattr(config, "operator_name", None)
    if not operator:
        return False
    return normalize_chat_user(user) == normalize_chat_user(operator)


# Extensions written as-is; anything else normalizes to .bin so a
# crafted subtype cannot choose an arbitrary filename suffix.
_DATA_URI_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
_DATA_URI_RE = re.compile(r"data:image/([a-zA-Z0-9.+-]+);base64,(.*)$",
                          re.S)


def decode_data_uri(data_uri):
    """(bytes, ext) for a `data:image/<ext>;base64,<payload>` URI, or
    (None, None) when it does not parse or decode. `ext` is normalized
    to one of the known image kinds, else `bin`."""
    m = _DATA_URI_RE.match(data_uri or "")
    if not m:
        return None, None
    try:
        payload = base64.b64decode(m.group(2), validate=True)
    except (ValueError, binascii.Error):
        return None, None
    ext = m.group(1).lower()
    if ext not in _DATA_URI_EXTENSIONS:
        ext = "bin"
    return payload, ext


def save_data_uri(home, data_uri, *, folder="images", name=None):
    """Decode a data: image URI to <home>/chat/<folder>/<name>.<ext> and
    return the Path, or None when it does not decode. `name` defaults to
    a fresh id; a caller with a natural one (a message row) passes it,
    so the file can be found again from the row alone."""
    payload, ext = decode_data_uri(data_uri)
    if payload is None:
        return None
    stem = name if name is not None else uuid.uuid4().hex[:12]
    target_dir = Path(home) / "chat" / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / ("%s.%s" % (stem, ext))
    path.write_bytes(payload)
    return path


_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_user     TEXT NOT NULL,
    user          TEXT NOT NULL,
    message       TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    type          TEXT NOT NULL,
    archived      INTEGER NOT NULL DEFAULT 0,
    reply_to      TEXT,
    reply_to_user TEXT,
    attachment_kind TEXT,   -- 'image' | 'voice' | 'video', or NULL
    attachment_path TEXT    -- absolute path to the asset file, or NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_user ON messages(chat_user);
CREATE INDEX IF NOT EXISTS idx_messages_archived  ON messages(archived);

CREATE TABLE IF NOT EXISTS reactions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL,
    user       TEXT NOT NULL,
    emoji      TEXT NOT NULL,
    tap_count  INTEGER NOT NULL DEFAULT 1,
    created    TEXT NOT NULL,
    UNIQUE(message_id, user, emoji)
);
CREATE INDEX IF NOT EXISTS idx_reactions_message ON reactions(message_id);

-- The change log a poll reads (#316): one counter for the database, a
-- message's `rev` set from it on every change a reader sees (the row
-- itself, its archive mark, its reactions), and a tombstone per deleted
-- row. Triggers keep it, so a writer that is not this class counts too.
CREATE TABLE IF NOT EXISTS chat_rev (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    n  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_gone (
    id        INTEGER PRIMARY KEY,
    chat_user TEXT NOT NULL,
    rev       INTEGER NOT NULL
);
"""

# Created after the `rev` column exists (an older database gains it in
# _add_reserved_columns). The update trigger names the columns a reader
# sees, so a delivery mark or a rev write does not count as a change.
_TRIGGERS = """
CREATE INDEX IF NOT EXISTS idx_messages_rev ON messages(rev) WHERE rev IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS chat_rev_message_insert AFTER INSERT ON messages BEGIN
    UPDATE chat_rev SET n = n + 1;
    UPDATE messages SET rev = (SELECT n FROM chat_rev) WHERE id = NEW.id;
    DELETE FROM chat_gone WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS chat_rev_message_update AFTER UPDATE OF
    chat_user, user, message, timestamp, type, archived, reply_to,
    reply_to_user, attachment_kind, attachment_path ON messages BEGIN
    UPDATE chat_rev SET n = n + 1;
    UPDATE messages SET rev = (SELECT n FROM chat_rev) WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS chat_rev_message_delete AFTER DELETE ON messages BEGIN
    UPDATE chat_rev SET n = n + 1;
    INSERT OR REPLACE INTO chat_gone (id, chat_user, rev)
        VALUES (OLD.id, OLD.chat_user, (SELECT n FROM chat_rev));
END;

CREATE TRIGGER IF NOT EXISTS chat_rev_reaction_insert AFTER INSERT ON reactions BEGIN
    UPDATE chat_rev SET n = n + 1;
    UPDATE messages SET rev = (SELECT n FROM chat_rev) WHERE id = NEW.message_id;
END;

CREATE TRIGGER IF NOT EXISTS chat_rev_reaction_update AFTER UPDATE ON reactions BEGIN
    UPDATE chat_rev SET n = n + 1;
    UPDATE messages SET rev = (SELECT n FROM chat_rev) WHERE id = NEW.message_id;
END;

CREATE TRIGGER IF NOT EXISTS chat_rev_reaction_delete AFTER DELETE ON reactions BEGIN
    UPDATE chat_rev SET n = n + 1;
    UPDATE messages SET rev = (SELECT n FROM chat_rev) WHERE id = OLD.message_id;
END;
"""


class ChatStore:
    """One connection over the chat database. The server opens one store
    per request and closes it explicitly when the request finishes."""

    def __init__(self, db_path):
        db_path = Path(db_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA wal_autocheckpoint=200")
            self.conn.executescript(_SCHEMA)
            self._add_reserved_columns()
            self.conn.commit()
        except BaseException:
            # A file that is not a database (or a schema that fails)
            # raises here; the caller never gets a store to close.
            self.conn.close()
            raise

    def _add_reserved_columns(self):
        """The attachment columns were reserved by the v1 chat spec and
        land with the media subsystem. A database created before media
        shipped lacks them; add them additively so its first
        attachment insert does not fail. Additive columns are the one
        anticipated migration - no id-space change, no data rewrite."""
        for column in ("attachment_kind", "attachment_path"):
            add_column(self.conn, "messages", column, "TEXT")
        # An inbound row waiting for its inbox put ('pending'), NULL once
        # the put returned (and for every row that is not delivered), and
        # the key that put runs under (chat_api.deliver_stored).
        add_column(self.conn, "messages", "delivery", "TEXT")
        add_column(self.conn, "messages", "delivery_key", "TEXT")
        # The change log's per-row mark (#316); NULL on a row older than
        # it, which a change poll never returns.
        add_column(self.conn, "messages", "rev", "INTEGER")
        # Seeded only when missing: a store opens per request, and a poll
        # should not take the write lock to learn the row is there.
        if self.conn.execute("SELECT 1 FROM chat_rev").fetchone() is None:
            self.conn.execute("INSERT OR IGNORE INTO chat_rev (id, n) VALUES (1, 0)")
        self.conn.executescript(_TRIGGERS)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_delivery"
                          " ON messages(delivery) WHERE delivery IS NOT NULL")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_delivery_key"
                          " ON messages(delivery_key) WHERE delivery_key IS NOT NULL")

    def close(self):
        self.conn.close()

    def add_message(
        self,
        *,
        chat_user,
        user,
        message,
        msg_type,
        reply_to=None,
        reply_to_user=None,
        attachment_kind=None,
        attachment_path=None,
        pending=False,
        delivery_key=None,
    ):
        """Insert one message row and return {"id", "timestamp"}. reply_to
        is opaque client JSON, stored verbatim. An attachment is a
        local asset path plus its kind; the bytes live on disk, the row
        holds only the path. `pending`: a row to be delivered, marked so
        until `mark_delivered` (chat_api.deliver_stored)."""
        timestamp = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "INSERT INTO messages"
            " (chat_user, user, message, timestamp, type, reply_to,"
            "  reply_to_user, attachment_kind, attachment_path, delivery, delivery_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_user, user, message, timestamp, msg_type, reply_to,
             reply_to_user, attachment_kind, attachment_path,
             "pending" if pending else None, delivery_key),
        )
        self.conn.commit()
        return {"id": cur.lastrowid, "timestamp": timestamp}

    def mark_delivered(self, message_id):
        self.conn.execute("UPDATE messages SET delivery = NULL WHERE id = ?", (message_id,))
        self.conn.commit()

    def pending_rows(self, older_than_iso):
        """Rows still waiting for their inbox put, stored before `older_than_iso`."""
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM messages WHERE delivery = 'pending' AND timestamp < ? ORDER BY id",
            (older_than_iso,))]

    def by_delivery_key(self, key):
        row = self.conn.execute("SELECT * FROM messages WHERE delivery_key = ? ORDER BY id"
                                " LIMIT 1", (key,)).fetchone()
        return dict(row) if row else None

    def delete_message(self, message_id):
        """Remove one row and its reactions: a message that was never
        delivered (chat_api.send) leaves no trace in the history."""
        self.conn.execute("DELETE FROM reactions WHERE message_id = ?", (message_id,))
        self.conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        self.conn.commit()

    def _archive_clause(self, archived):
        """WHERE fragment for the archived filter: live rows by default,
        archived-only on '1', everything on 'all'."""
        if archived == "all":
            return ""
        return " AND archived=%d" % (1 if archived == "1" else 0)

    def _reactions_for(self, message_ids):
        """Map of message id -> reaction rows, one query for the batch."""
        if not message_ids:
            return {}
        marks = ",".join("?" * len(message_ids))
        out = {mid: [] for mid in message_ids}
        for row in self.conn.execute(
            "SELECT message_id, user, emoji, tap_count FROM reactions"
            " WHERE message_id IN (%s) ORDER BY id" % marks,
            list(message_ids),
        ):
            out[row["message_id"]].append(
                {"user": row["user"], "emoji": row["emoji"],
                 "tap_count": row["tap_count"]}
            )
        return out

    def rev(self):
        """The change log's counter: every change a reader sees raises it."""
        return self.conn.execute("SELECT n FROM chat_rev").fetchone()[0]

    def changes(self, user, *, changed, limit=500, archived="0"):
        """One thread's rows changed after rev `changed` (new, edited,
        archived, reacted to), whatever their archive mark, so a reader
        drops the ones that left its view; `gone` names the rows deleted
        since. More than `limit` changed rows is `reset`: read the thread
        again. The counter is read first, so a change racing the read is
        sent again on the next poll rather than missed."""
        chat_user = normalize_chat_user(user)
        rev = self.rev()
        total = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE chat_user=?"
            + self._archive_clause(archived), [chat_user]).fetchone()[0]
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE chat_user=? AND rev>?"
            " ORDER BY id LIMIT ?", (chat_user, changed, limit + 1)).fetchall()
        if len(rows) > limit:
            return {"messages": [], "gone": [], "rev": rev, "total": total,
                    "has_more": False, "reset": True}
        messages = [dict(r) for r in rows]
        reactions = self._reactions_for([m["id"] for m in messages])
        for m in messages:
            m["reactions"] = reactions.get(m["id"], [])
        gone = [r[0] for r in self.conn.execute(
            "SELECT id FROM chat_gone WHERE chat_user=? AND rev>? ORDER BY id",
            (chat_user, changed))]
        return {"messages": messages, "gone": gone, "rev": rev, "total": total,
                "has_more": False}

    def history(self, user, *, since=None, before=None, limit=200,
                archived="0"):
        """One thread's messages. `before` pages backward (rows below that
        id), `since` polls forward (rows above it), neither returns the
        newest `limit`. Rows always come back oldest-first; has_more is
        computed from the thread's actual row count, not guessed from a
        full page. `rev` is the change log's counter, read before the
        rows: the cursor a `changes` poll starts from."""
        chat_user = normalize_chat_user(user)
        rev = self.rev()
        where = "chat_user=?" + self._archive_clause(archived)
        args = [chat_user]
        total = self.conn.execute(
            "SELECT COUNT(*) FROM messages WHERE " + where, args
        ).fetchone()[0]
        # has_more answers "is there more in the direction you are paging",
        # so the window is counted per mode, before the LIMIT truncates it.
        if since is not None:
            where += " AND id>?"
            args.append(since)
            window = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE " + where, args
            ).fetchone()[0]
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE %s ORDER BY id LIMIT ?" % where,
                args + [limit],
            ).fetchall()
        else:
            if before is not None:
                where += " AND id<?"
                args.append(before)
            window = self.conn.execute(
                "SELECT COUNT(*) FROM messages WHERE " + where, args
            ).fetchone()[0]
            rows = self.conn.execute(
                "SELECT * FROM messages WHERE %s ORDER BY id DESC"
                " LIMIT ?" % where,
                args + [limit],
            ).fetchall()
            rows = list(reversed(rows))
        messages = [dict(r) for r in rows]
        reactions = self._reactions_for([m["id"] for m in messages])
        for m in messages:
            m["reactions"] = reactions.get(m["id"], [])
        return {
            "messages": messages,
            "total": total,
            "has_more": window > len(messages),
            "rev": rev,
        }

    def search(self, q, *, user=None, archived="0", cap=50):
        """Substring search over message text and the quoted text inside
        reply_to. Without `user` it spans all threads. Newest-first,
        capped: search answers "where did we say this", not "export the
        archive"."""
        like = "%" + q + "%"
        where = "(message LIKE ? OR reply_to LIKE ?)"
        args = [like, like]
        if user is not None:
            where += " AND chat_user=?"
            args.append(normalize_chat_user(user))
        where += self._archive_clause(archived)
        rows = self.conn.execute(
            "SELECT * FROM messages WHERE %s ORDER BY id DESC"
            " LIMIT ?" % where,
            args + [cap],
        ).fetchall()
        return [dict(r) for r in rows]

    def react(self, message_id, *, user, emoji, action):
        """Apply a reaction and return the message's full reaction state.
        A tap on an existing reaction BUMPS its tap_count - repeated taps
        are an urgency signal, never a toggle-off. Only an explicit
        'remove' deletes."""
        if action == "remove":
            self.conn.execute(
                "DELETE FROM reactions"
                " WHERE message_id=? AND user=? AND emoji=?",
                (message_id, user, emoji),
            )
            op = "removed"
        else:
            existing = self.conn.execute(
                "SELECT id FROM reactions"
                " WHERE message_id=? AND user=? AND emoji=?",
                (message_id, user, emoji),
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE reactions SET tap_count=tap_count+1 WHERE id=?",
                    (existing["id"],),
                )
                op = "bumped"
            else:
                self.conn.execute(
                    "INSERT INTO reactions"
                    " (message_id, user, emoji, created)"
                    " VALUES (?, ?, ?, ?)",
                    (message_id, user, emoji,
                     datetime.now(timezone.utc).isoformat()),
                )
                op = "added"
        self.conn.commit()
        state = self._reactions_for([message_id])[message_id]
        return {"message_id": message_id, "op": op, "reactions": state}

    def archive(self, user, *, keep=0):
        """Archive all but the newest `keep` live rows of one thread and
        return how many rows actually changed."""
        chat_user = normalize_chat_user(user)
        cur = self.conn.execute(
            "UPDATE messages SET archived=1"
            " WHERE chat_user=? AND archived=0 AND id NOT IN ("
            "   SELECT id FROM messages WHERE chat_user=? AND archived=0"
            "   ORDER BY id DESC LIMIT ?)",
            (chat_user, chat_user, keep),
        )
        self.conn.commit()
        return cur.rowcount
