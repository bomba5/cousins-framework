"""The databases several processes write are in WAL mode.

Six modules set `journal_mode=WAL` and four did not, with no reason for
the split: jobs.db (every cousin's hooks, every backgrounded shell),
scheduled.db, hive.db and each cousin's memory index. In the rollback
journal a writer blocks readers as well, and jobs.db is the most
contended file on the host.
"""
import pathlib
import sqlite3
import tempfile
import unittest


def journal_mode(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        conn.close()


class TestWal(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        (self.root / "config").mkdir()
        (self.root / "data").mkdir()

    def _env(self):
        from unittest import mock
        return mock.patch.dict("os.environ",
                               {"FRAMEWORK_ROOT": str(self.root)})

    def test_the_jobs_database_is_wal(self):
        from cousin_lib import jobs
        with self._env():
            jobs._db().close()
        self.assertEqual(journal_mode(self.root / "data" / "jobs.db"), "wal")

    def test_the_schedule_database_is_wal(self):
        from cousin_lib import schedule
        with self._env():
            schedule._db().close()
        self.assertEqual(
            journal_mode(self.root / "data" / "scheduled.db"), "wal")

    def test_the_hive_store_is_wal(self):
        from cousin_lib.hive import HiveStore
        store = HiveStore(self.root / "data")
        self.addCleanup(store.close)
        self.assertEqual(journal_mode(self.root / "data" / "hive.db"), "wal")


if __name__ == "__main__":
    unittest.main()


class TestMemoryIndexWal(unittest.TestCase):
    """The loops daemon refreshes every home's index while the cousin's
    own search reads it, so this file has two writers by design."""

    def test_the_memory_index_is_wal(self):
        import tempfile
        from cousin_lib import memory_search
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = pathlib.Path(tmp.name)
        (home / "memory").mkdir(parents=True)
        (home / "memory" / "a.md").write_text("# a\n\nsome text\n")
        memory_search.build_index(home)
        self.assertEqual(
            journal_mode(memory_search._fts_path(home)), "wal")
