"""Chat storage: schema, WAL policy, and thread history semantics.

Tested against real SQLite files in temporary directories - the module's
whole job is what it persists, so nothing here is mocked.
"""
import gc
import pathlib
import sqlite3
import tempfile
import unittest
import warnings

from cousin_lib.server.storage import ChatStore, normalize_chat_user


class StoreCase(unittest.TestCase):
    def _db_path(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return pathlib.Path(tmp.name) / "data" / "chat.db"

    def _store(self):
        store = ChatStore(self._db_path())
        self.addCleanup(store.close)
        return store


class TestCreation(StoreCase):
    def test_creates_full_schema_on_first_open(self):
        store = self._store()
        rows = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        tables = {r[0] for r in rows}
        self.assertIn("messages", tables)
        self.assertIn("reactions", tables)

    def test_database_is_wal_mode_with_small_autocheckpoint(self):
        store = self._store()
        mode = store.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode, "wal")
        pages = store.conn.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
        self.assertEqual(pages, 200)

    def test_creates_parent_directory_when_missing(self):
        # The server opens <home>/data/chat.db before anything else has
        # created <home>/data.
        path = self._db_path()
        self.assertFalse(path.parent.exists())
        store = ChatStore(path)
        self.addCleanup(store.close)
        self.assertTrue(path.is_file())


class TestOpenFailure(StoreCase):
    def test_a_file_that_is_not_a_database_leaves_no_connection_open(self):
        # The connection opens before the schema runs; when the schema
        # fails the caller has no store to close, so the store must.
        path = self._db_path()
        path.parent.mkdir(parents=True)
        path.write_bytes(b"not a database, just some text " * 64)
        gc.collect()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with self.assertRaises(sqlite3.DatabaseError):
                ChatStore(path)
            gc.collect()
        self.assertEqual([str(w.message) for w in caught
                          if issubclass(w.category, ResourceWarning)], [])


class TestNormalization(StoreCase):
    def test_lowercases_and_replaces_spaces_with_underscores(self):
        self.assertEqual(normalize_chat_user("Sam Vimes"), "sam_vimes")

    def test_empty_name_maps_to_unknown(self):
        self.assertEqual(normalize_chat_user(""), "unknown")
        self.assertEqual(normalize_chat_user(None), "unknown")


class TestAddMessage(StoreCase):
    def test_returns_id_and_utc_iso_timestamp(self):
        store = self._store()
        row = store.add_message(
            chat_user="sam", user="Sam", message="hello", msg_type="user"
        )
        self.assertIsInstance(row["id"], int)
        # ISO-8601 UTC: parseable and explicitly offset-aware.
        from datetime import datetime, timezone

        ts = datetime.fromisoformat(row["timestamp"])
        self.assertEqual(ts.utcoffset(), timezone.utc.utcoffset(None))

    def test_reply_to_json_is_stored_verbatim(self):
        # reply_to is opaque client JSON; the store neither parses nor
        # reshapes it.
        store = self._store()
        blob = '{"id": 7, "text": "quoted words"}'
        row = store.add_message(
            chat_user="sam",
            user="Sam",
            message="answer",
            msg_type="user",
            reply_to=blob,
        )
        stored = store.conn.execute(
            "SELECT reply_to FROM messages WHERE id=?", (row["id"],)
        ).fetchone()[0]
        self.assertEqual(stored, blob)


class TestHistory(StoreCase):
    def _seed(self, store, n, chat_user="sam", user="Sam"):
        ids = []
        for i in range(n):
            row = store.add_message(
                chat_user=chat_user, user=user,
                message="m%d" % i, msg_type="user",
            )
            ids.append(row["id"])
        return ids

    def test_default_mode_returns_newest_limit_oldest_first(self):
        store = self._store()
        ids = self._seed(store, 5)
        out = store.history("Sam", limit=3)
        got = [m["id"] for m in out["messages"]]
        self.assertEqual(got, ids[2:])  # newest 3, oldest-first
        self.assertEqual(out["total"], 5)
        self.assertTrue(out["has_more"])

    def test_before_pages_backward_returning_oldest_first(self):
        store = self._store()
        ids = self._seed(store, 5)
        out = store.history("Sam", before=ids[3], limit=2)
        got = [m["id"] for m in out["messages"]]
        self.assertEqual(got, ids[1:3])  # the 2 rows below ids[3], ascending

    def test_since_polls_forward(self):
        store = self._store()
        ids = self._seed(store, 5)
        out = store.history("Sam", since=ids[2])
        got = [m["id"] for m in out["messages"]]
        self.assertEqual(got, ids[3:])
        self.assertFalse(out["has_more"])

    def test_history_is_per_thread_via_normalized_key(self):
        store = self._store()
        self._seed(store, 2, chat_user="sam", user="Sam")
        self._seed(store, 3, chat_user="pat", user="Pat")
        out = store.history("Sam")
        self.assertEqual(out["total"], 2)

    def test_archived_rows_hidden_by_default_and_selectable(self):
        store = self._store()
        ids = self._seed(store, 4)
        store.conn.execute(
            "UPDATE messages SET archived=1 WHERE id IN (?, ?)",
            (ids[0], ids[1]),
        )
        store.conn.commit()
        self.assertEqual(store.history("Sam")["total"], 2)
        self.assertEqual(store.history("Sam", archived="1")["total"], 2)
        self.assertEqual(store.history("Sam", archived="all")["total"], 4)

    def test_messages_carry_a_reactions_list(self):
        store = self._store()
        self._seed(store, 1)
        msg = store.history("Sam")["messages"][0]
        self.assertEqual(msg["reactions"], [])


class TestSearch(StoreCase):
    def test_substring_match_on_message_text(self):
        store = self._store()
        store.add_message(chat_user="sam", user="Sam",
                          message="the gate bites", msg_type="user")
        store.add_message(chat_user="sam", user="Sam",
                          message="unrelated", msg_type="user")
        hits = store.search("gate")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["message"], "the gate bites")

    def test_searches_all_threads_unless_user_given(self):
        store = self._store()
        store.add_message(chat_user="sam", user="Sam",
                          message="topic x", msg_type="user")
        store.add_message(chat_user="pat", user="Pat",
                          message="topic x", msg_type="user")
        self.assertEqual(len(store.search("topic x")), 2)
        self.assertEqual(len(store.search("topic x", user="Sam")), 1)

    def test_matches_quoted_text_in_reply_to(self):
        store = self._store()
        store.add_message(
            chat_user="sam", user="Sam", message="see above",
            msg_type="user",
            reply_to='{"text": "the memorable phrase"}',
        )
        self.assertEqual(len(store.search("memorable phrase")), 1)

    def test_newest_first_capped_at_50(self):
        store = self._store()
        for i in range(55):
            store.add_message(chat_user="sam", user="Sam",
                              message="needle %d" % i, msg_type="user")
        hits = store.search("needle")
        self.assertEqual(len(hits), 50)
        self.assertEqual(hits[0]["message"], "needle 54")

    def test_archive_filter_applies_as_in_history(self):
        store = self._store()
        row = store.add_message(chat_user="sam", user="Sam",
                                message="old needle", msg_type="user")
        store.add_message(chat_user="sam", user="Sam",
                          message="live needle", msg_type="user")
        store.conn.execute(
            "UPDATE messages SET archived=1 WHERE id=?", (row["id"],)
        )
        store.conn.commit()
        self.assertEqual(len(store.search("needle")), 1)
        self.assertEqual(len(store.search("needle", archived="all")), 2)


class TestReactions(StoreCase):
    def _msg(self, store):
        return store.add_message(chat_user="sam", user="Sam",
                                 message="hello", msg_type="user")["id"]

    def test_first_tap_adds_with_count_one(self):
        store = self._store()
        mid = self._msg(store)
        out = store.react(mid, user="Sam", emoji="🔥", action="tap")
        self.assertEqual(out["op"], "added")
        self.assertEqual(out["reactions"],
                         [{"user": "Sam", "emoji": "🔥", "tap_count": 1}])

    def test_retap_bumps_instead_of_toggling_off(self):
        # Repeated taps are an urgency signal; the unique constraint must
        # not dedupe them and a retap must never remove.
        store = self._store()
        mid = self._msg(store)
        store.react(mid, user="Sam", emoji="🔥", action="tap")
        out = store.react(mid, user="Sam", emoji="🔥", action="tap")
        self.assertEqual(out["op"], "bumped")
        self.assertEqual(out["reactions"][0]["tap_count"], 2)

    def test_remove_deletes_the_reaction(self):
        store = self._store()
        mid = self._msg(store)
        store.react(mid, user="Sam", emoji="🔥", action="tap")
        out = store.react(mid, user="Sam", emoji="🔥", action="remove")
        self.assertEqual(out["op"], "removed")
        self.assertEqual(out["reactions"], [])

    def test_reactions_appear_on_history_messages(self):
        store = self._store()
        mid = self._msg(store)
        store.react(mid, user="Pat", emoji="👍", action="tap")
        msg = store.history("Sam")["messages"][0]
        self.assertEqual(msg["reactions"],
                         [{"user": "Pat", "emoji": "👍", "tap_count": 1}])


class TestArchive(StoreCase):
    def _seed(self, store, n):
        for i in range(n):
            store.add_message(chat_user="sam", user="Sam",
                              message="m%d" % i, msg_type="user")

    def test_archives_all_but_newest_keep(self):
        store = self._store()
        self._seed(store, 5)
        archived = store.archive("Sam", keep=2)
        self.assertEqual(archived, 3)
        out = store.history("Sam")
        self.assertEqual([m["message"] for m in out["messages"]],
                         ["m3", "m4"])

    def test_keep_zero_archives_the_whole_thread(self):
        store = self._store()
        self._seed(store, 3)
        self.assertEqual(store.archive("Sam", keep=0), 3)
        self.assertEqual(store.history("Sam")["total"], 0)

    def test_already_archived_rows_are_not_recounted(self):
        store = self._store()
        self._seed(store, 3)
        store.archive("Sam", keep=1)
        self.assertEqual(store.archive("Sam", keep=1), 0)


class TestAttachments(StoreCase):
    def test_message_carries_an_attachment_path_through_history(self):
        store = self._store()
        store.add_message(
            chat_user="sam", user="Sam", message="look",
            msg_type="user", attachment_kind="image",
            attachment_path="/srv/chat/images/wren_1_ab.png")
        msg = store.history("Sam")["messages"][0]
        self.assertEqual(msg["attachment_kind"], "image")
        self.assertEqual(msg["attachment_path"],
                         "/srv/chat/images/wren_1_ab.png")

    def test_a_message_without_an_attachment_reports_none(self):
        store = self._store()
        store.add_message(chat_user="sam", user="Sam", message="hi",
                          msg_type="user")
        msg = store.history("Sam")["messages"][0]
        self.assertIsNone(msg["attachment_kind"])
        self.assertIsNone(msg["attachment_path"])

    def test_a_pre_media_database_gains_the_columns(self):
        # A chat.db created before media shipped has no attachment
        # columns; opening it must add them additively rather than
        # fail the first attachment insert. The reserved columns
        # becoming real is the one anticipated schema change.
        import sqlite3
        path = self._db_path()
        path.parent.mkdir(parents=True)
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY,"
            " chat_user TEXT, user TEXT, message TEXT, timestamp TEXT,"
            " type TEXT, archived INTEGER DEFAULT 0, reply_to TEXT,"
            " reply_to_user TEXT)")
        con.commit()
        con.close()
        store = ChatStore(path)
        self.addCleanup(store.close)
        cols = {r[1] for r in store.conn.execute(
            "PRAGMA table_info(messages)")}
        self.assertIn("attachment_kind", cols)
        self.assertIn("attachment_path", cols)


if __name__ == "__main__":
    unittest.main()
