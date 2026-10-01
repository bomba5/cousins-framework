"""cousin-chat-import: a cousin's chat history from the previous framework.

The previous framework's messages table carries original_id and three
media columns (image, audio, video); this one carries attachment_kind and
attachment_path, and the console shows a message's picture from
<home>/chat/inbound/<message id>.<ext>. The import keeps every old row's
id (so its reply quotes still point at the right message), moves the few
rows the new home already holds to ids after the old ones (renaming their
inbound files and fixing reply quotes that point at them), and copies old
pictures under their message id.

Canary: a migration that moves memory must not leave chat history
behind; it comes along with every cousin.
"""
import base64
import json
import pathlib
import sqlite3
import tempfile
import unittest

from cousin_lib import chat_import

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

OLD_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_user TEXT NOT NULL, original_id INTEGER, user TEXT NOT NULL,
    message TEXT NOT NULL, timestamp TEXT NOT NULL, type TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    reply_to TEXT, reply_to_user TEXT, image TEXT, audio TEXT, video TEXT);
CREATE TABLE reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
    user TEXT NOT NULL, emoji TEXT NOT NULL, created TEXT NOT NULL,
    tap_count INTEGER NOT NULL DEFAULT 1, UNIQUE(message_id, user, emoji));
"""

NEW_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_user TEXT NOT NULL, user TEXT NOT NULL, message TEXT NOT NULL,
    timestamp TEXT NOT NULL, type TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    reply_to TEXT, reply_to_user TEXT,
    attachment_kind TEXT, attachment_path TEXT);
CREATE TABLE reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, message_id INTEGER NOT NULL,
    user TEXT NOT NULL, emoji TEXT NOT NULL,
    tap_count INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL,
    UNIQUE(message_id, user, emoji));
"""


class ImportCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = pathlib.Path(tmp.name)
        self.old_home = base / "old"
        self.new_home = base / "new"
        (self.old_home / "data").mkdir(parents=True)
        (self.old_home / "chat" / "images").mkdir(parents=True)
        (self.new_home / "data").mkdir(parents=True)
        (self.new_home / "chat" / "inbound").mkdir(parents=True)
        (self.old_home / "chat" / "images" / "render.png").write_bytes(PNG)

        old = sqlite3.connect(self.old_home / "data" / "chat.db")
        old.executescript(OLD_SCHEMA)
        rows = [
            (1, "operator", None, "Operator", "hi", "2026-05-01T10:00:00", "user", 1, None, None, None),
            (2, "operator", 2, "Wren", "a render", "2026-05-01T10:01:00", "wren", 1, None, "Operator",
             "/api/chat/image/render.png"),
            (4, "operator", None, "Operator", "a photo", "2026-05-01T10:02:00", "user", 0,
             json.dumps({"id": 2}), None,
             "data:image/png;base64," + base64.b64encode(PNG).decode()),
            (5, "kestrel", None, "Kestrel", "peer note", "2026-05-01T10:03:00", "user", 0, None, None,
             "/api/chat/image/gone.png"),
        ]
        old.executemany(
            "INSERT INTO messages (id, chat_user, original_id, user, message,"
            " timestamp, type, archived, reply_to, reply_to_user, image)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        old.execute("INSERT INTO reactions (message_id, user, emoji, created)"
                    " VALUES (2, 'Operator', 'thumbs', '2026-05-01T10:05:00')")
        old.commit()
        old.close()

        new = sqlite3.connect(self.new_home / "data" / "chat.db")
        new.executescript(NEW_SCHEMA)
        new.executemany(
            "INSERT INTO messages (id, chat_user, user, message, timestamp,"
            " type, reply_to) VALUES (?,?,?,?,?,?,?)",
            [(1, "operator", "Operator", "first on new", "2026-09-18T10:00:00", "user", None),
             (2, "operator", "Wren", "reply on new", "2026-09-18T10:00:05", "wren",
              json.dumps({"id": 1}))])
        new.execute("INSERT INTO reactions (message_id, user, emoji, created)"
                    " VALUES (1, 'Operator', 'wave', '2026-09-18T10:01:00')")
        new.commit()
        new.close()
        (self.new_home / "chat" / "inbound" / "1.jpg").write_bytes(b"newpic")

    def _rows(self):
        c = sqlite3.connect(self.new_home / "data" / "chat.db")
        c.row_factory = sqlite3.Row
        return {r["id"]: dict(r) for r in c.execute("SELECT * FROM messages")}

    def _run(self, **kw):
        return chat_import.import_history(self.old_home, self.new_home, **kw)


class TestRefusesWhileTheRunnerRuns(ImportCase):
    def test_import_refuses_while_the_runner_runs(self):
        # no chat server to probe: the cousin's runner holding its
        # lock is what "running" means, and the import waits for a stop
        import contextlib
        import io
        from unittest import mock
        from cousin_lib.runner.main import hold_lock
        root = self.new_home.parent / "root"
        home = root / "cousins" / "wren"
        home.mkdir(parents=True)
        (home / "cousin.toml").write_text(
            '[cousin]\nslug = "wren"\nname = "Wren"\n[agent]\nrunner = "fake"\n')
        argv = ["wren", "--old-home", str(self.old_home), "--root", str(root)]
        err = io.StringIO()
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with hold_lock(home):
                self.assertEqual(chat_import.main(argv), 2)
            self.assertIn("runner", err.getvalue())
            self.assertFalse((home / "data" / chat_import.MARKER).exists())
            (home / "data").mkdir(exist_ok=True)
            self.assertEqual(chat_import.main(argv), 0)
        self.assertTrue((home / "data" / chat_import.MARKER).exists())


class TestImport(ImportCase):
    def test_old_rows_keep_their_ids_and_new_rows_follow(self):
        report = self._run()
        rows = self._rows()
        self.assertEqual(sorted(rows), [1, 2, 4, 5, 6, 7])
        self.assertEqual(rows[1]["message"], "hi")
        self.assertEqual(rows[6]["message"], "first on new")
        self.assertEqual(rows[7]["message"], "reply on new")
        self.assertEqual(report["imported"], 4)
        self.assertEqual(report["renumbered"], 2)

    def test_reply_quotes_follow_their_messages(self):
        self._run()
        rows = self._rows()
        self.assertEqual(json.loads(rows[4]["reply_to"])["id"], 2)
        self.assertEqual(json.loads(rows[7]["reply_to"])["id"], 6)

    def test_old_pictures_land_under_their_message_id(self):
        report = self._run()
        inbound = self.new_home / "chat" / "inbound"
        self.assertEqual((inbound / "2.png").read_bytes(), PNG)
        self.assertEqual((inbound / "4.png").read_bytes(), PNG)
        rows = self._rows()
        self.assertEqual(rows[2]["attachment_kind"], "image")
        self.assertEqual(rows[2]["attachment_path"], str(inbound / "2.png"))
        self.assertEqual(report["images_missing"], 1)
        self.assertIsNone(rows[5]["attachment_kind"])

    def test_new_inbound_files_move_with_their_rows(self):
        self._run()
        inbound = self.new_home / "chat" / "inbound"
        self.assertFalse((inbound / "1.jpg").exists())
        self.assertEqual((inbound / "6.jpg").read_bytes(), b"newpic")

    def test_reactions_follow_their_messages(self):
        self._run()
        c = sqlite3.connect(self.new_home / "data" / "chat.db")
        got = sorted(c.execute("SELECT message_id, emoji FROM reactions"))
        self.assertEqual(got, [(2, "thumbs"), (6, "wave")])

    def test_the_sequence_continues_after_the_highest_id(self):
        self._run()
        c = sqlite3.connect(self.new_home / "data" / "chat.db")
        c.execute("INSERT INTO messages (chat_user, user, message, timestamp,"
                  " type) VALUES ('operator', 'Operator', 'next', 't', 'user')")
        self.assertEqual(c.execute("SELECT max(id) FROM messages").fetchone()[0], 8)

    def test_a_second_import_is_refused(self):
        self._run()
        with self.assertRaises(chat_import.ImportRefused):
            self._run()
        self.assertEqual(len(self._rows()), 6)

    def test_a_backup_of_the_new_store_is_kept(self):
        self._run()
        backups = list((self.new_home / "data").glob("chat.db.pre-import-*"))
        self.assertEqual(len(backups), 1)

    def test_a_missing_new_store_is_created_first(self):
        # Canary: a cousin whose chat server never wrote a
        # row has no store yet; the import creates it instead of refusing.
        (self.new_home / "data" / "chat.db").unlink()
        report = self._run()
        self.assertEqual(report["imported"], 4)
        self.assertEqual(report["renumbered"], 0)
        self.assertEqual(sorted(self._rows()), [1, 2, 4, 5])

    def test_a_missing_old_store_is_refused(self):
        (self.old_home / "data" / "chat.db").unlink()
        with self.assertRaises(chat_import.ImportRefused):
            self._run()


if __name__ == "__main__":
    unittest.main()
