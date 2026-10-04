"""Additive column migrations must survive two processes opening the same
database at once.

Every one of them read `PRAGMA table_info` and then issued `ALTER TABLE
ADD COLUMN` if the column was missing. That is check-then-act across
connections: two openers both see it missing, both alter, and the loser
gets `OperationalError: duplicate column name`. It reached us as a flaky
console test (`duplicate column name: vec_model`), but the
same shape sits in the jobs database that every cousin's hooks open and
in each chat server's message store.
"""
import sqlite3
import tempfile
import pathlib
import unittest

from cousin_lib.sqlite_util import add_column


class TestAddColumn(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = pathlib.Path(tmp.name) / "x.db"
        self.conn = sqlite3.connect(self.path)
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    def test_it_adds_a_missing_column(self):
        add_column(self.conn, "t", "vec_model", "TEXT")
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(t)")}
        self.assertIn("vec_model", cols)

    def test_adding_a_column_that_exists_is_a_no_op(self):
        add_column(self.conn, "t", "vec_model", "TEXT")
        add_column(self.conn, "t", "vec_model", "TEXT")
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(t)")]
        self.assertEqual(cols.count("vec_model"), 1)

    def test_a_column_another_connection_added_is_a_no_op(self):
        other = sqlite3.connect(self.path)
        self.addCleanup(other.close)
        other.execute("ALTER TABLE t ADD COLUMN vec_model TEXT")
        other.commit()
        add_column(self.conn, "t", "vec_model", "TEXT")

    def test_a_duplicate_alter_does_not_wait_for_the_write_lock(self):
        """jobs._db() re-runs its migration on every call, so this decides
        whether every read of that database becomes a writer. A duplicate
        ALTER fails while the statement is prepared, before the locking
        stage: it returns at once even while another connection holds
        RESERVED, where a real write waits out the busy timeout."""
        add_column(self.conn, "t", "vec_model", "TEXT")
        self.conn.commit()
        holder = sqlite3.connect(self.path, timeout=0.2)
        self.addCleanup(holder.close)
        holder.execute("BEGIN IMMEDIATE")   # RESERVED: real writers block
        self.addCleanup(holder.execute, "ROLLBACK")
        other = sqlite3.connect(self.path, timeout=0.2)
        self.addCleanup(other.close)
        with self.assertRaises(sqlite3.OperationalError) as cm:
            other.execute("INSERT INTO t (id) VALUES (1)")
        self.assertIn("locked", str(cm.exception))   # the lock is held
        add_column(other, "t", "vec_model", "TEXT")  # and this sails past

    def test_any_other_error_still_raises(self):
        with self.assertRaises(sqlite3.OperationalError):
            add_column(self.conn, "no_such_table", "c", "TEXT")


if __name__ == "__main__":
    unittest.main()
