"""Backup snapshots: a readable copy of a cousin home's databases and
memory, taken without touching the source.

The databases go through VACUUM INTO (a consistent copy even while the
chat server holds the file open); memory and the core markdown files
are plain copies. Rebuildable search indexes are not memory and are
skipped. The destination is an argument: a default backup path in code
is somebody's disk.
"""
import contextlib
import io
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from cousin_lib import backup
from cousin_lib.backup import backup_main


class BackupCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)
        self.home = self.root / "cousins" / "testa"
        (self.home / "data").mkdir(parents=True)
        (self.home / "cousin.toml").write_text(
            '[cousin]\nslug = "testa"\n[chat]\nport = 8100\n')
        self.db = self.home / "data" / "chat.db"
        con = sqlite3.connect(self.db)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE k (id INTEGER, v TEXT)")
        con.execute("INSERT INTO k VALUES (1, 'one'), (2, 'two')")
        con.commit()
        con.close()
        self.dest = self.root / "backups"
        patcher = mock.patch.dict(os.environ,
                                  {"COUSIN_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = backup_main(argv)
        return rc, out.getvalue(), err.getvalue()


class TestSnapshot(BackupCase):
    def test_database_copy_is_readable_and_source_untouched(self):
        before = self.db.stat().st_mtime_ns
        snap = backup.snapshot(self.home, self.dest)
        self.assertEqual(snap.parent, self.dest / "testa")
        self.assertRegex(snap.name, r"^\d{4}-\d{2}-\d{2}$")
        copy = snap / "data" / "chat.db"
        con = sqlite3.connect(copy)
        rows = con.execute("SELECT v FROM k ORDER BY id").fetchall()
        con.close()
        self.assertEqual(rows, [("one",), ("two",)])
        self.assertEqual(self.db.stat().st_mtime_ns, before)
        leftovers = [p.name for p in (self.home / "data").iterdir()
                     if p.name.startswith("chat.db") and
                     p.suffix not in (".db", ".db-wal", ".db-shm")]
        self.assertEqual(leftovers, [], "temp files left beside the source")
        self.assertEqual([p.name for p in copy.parent.iterdir()],
                         ["chat.db"], "temp file left in the snapshot")

    def test_every_db_under_data_is_snapshotted_with_its_relpath(self):
        nested = self.home / "data" / "sub" / "jobs.db"
        nested.parent.mkdir()
        con = sqlite3.connect(nested)
        con.execute("CREATE TABLE j (x)")
        con.commit()
        con.close()
        snap = backup.snapshot(self.home, self.dest)
        self.assertTrue((snap / "data" / "sub" / "jobs.db").is_file())
        con = sqlite3.connect(snap / "data" / "sub" / "jobs.db")
        self.assertEqual(con.execute("SELECT count(*) FROM j").fetchone(),
                         (0,))
        con.close()

    def test_memory_and_core_files_are_plain_copies(self):
        (self.home / "memory" / "deep").mkdir(parents=True)
        (self.home / "memory" / "fact.md").write_text("a fact\n")
        (self.home / "memory" / "deep" / "more.md").write_text("more\n")
        (self.home / "memory" / "fts_index.db").write_text("rebuildable")
        (self.home / "memory" / "embeddings.json").write_text("{}")
        (self.home / "MEMORY.md").write_text("timeline\n")
        (self.home / "CLAUDE.md").write_text("bedrock\n")
        # no STATUS.md: absent core files are skipped, not an error
        snap = backup.snapshot(self.home, self.dest)
        self.assertEqual((snap / "memory" / "fact.md").read_text(), "a fact\n")
        self.assertEqual((snap / "memory" / "deep" / "more.md").read_text(),
                         "more\n")
        self.assertFalse((snap / "memory" / "fts_index.db").exists())
        self.assertFalse((snap / "memory" / "embeddings.json").exists())
        self.assertEqual((snap / "MEMORY.md").read_text(), "timeline\n")
        self.assertEqual((snap / "CLAUDE.md").read_text(), "bedrock\n")
        self.assertFalse((snap / "STATUS.md").exists())

    def test_second_run_on_the_same_day_overwrites_in_place(self):
        first = backup.snapshot(self.home, self.dest)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO k VALUES (3, 'three')")
        con.commit()
        con.close()
        second = backup.snapshot(self.home, self.dest)
        self.assertEqual(first, second)
        con = sqlite3.connect(second / "data" / "chat.db")
        self.assertEqual(con.execute("SELECT count(*) FROM k").fetchone(),
                         (3,))
        con.close()

    def test_home_without_identity_is_refused(self):
        (self.home / "cousin.toml").unlink()
        with self.assertRaises(backup.BackupError):
            backup.snapshot(self.home, self.dest)
        self.assertFalse(self.dest.exists())


class TestCli(BackupCase):
    def test_dest_is_required(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                backup_main([])
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(self.dest.exists())

    def test_success_prints_the_snapshot_path(self):
        rc, out, _ = self._main(["--dest", str(self.dest)])
        self.assertEqual(rc, 0)
        self.assertIn(str(self.dest / "testa"), out)
        self.assertTrue((self.dest / "testa").is_dir())

    def test_no_context_refuses(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            rc, _, err = self._main(["--dest", str(self.dest)])
        self.assertEqual(rc, 2)
        self.assertIn("COUSIN_HOME", err)
        self.assertFalse(self.dest.exists())

    def test_missing_identity_is_a_config_error(self):
        (self.home / "cousin.toml").unlink()
        rc, _, err = self._main(["--dest", str(self.dest)])
        self.assertEqual(rc, 2)
        self.assertIn("cousin.toml", err)


if __name__ == "__main__":
    unittest.main()
