"""The vector store: SQLite with float32 blobs, not a JSON file.

The index was one JSON object read and parsed in full on every search.
Measured 2026-09-21 on a real cousin: 31.6 MB and 1283 ms per query,
against 938 ms for the embedding call it exists to serve, and linear
in the size of the memory. The numbers are the whole reason for this
module; the tests below are about the properties that must survive the
change of storage.
"""
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest
from unittest import mock

from cousin_lib import memory_search
from tests._hermetic import HermeticCase


class VectorStoreCase(HermeticCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = pathlib.Path(tmp.name) / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)


class TestRoundTrip(VectorStoreCase):
    def test_a_vector_survives_a_round_trip(self):
        index = {"memory:a.md#0": {"mtime": 12.5, "text_hash": "abc",
                                   "vector": [0.25, -0.5, 0.75]}}
        memory_search._write_index_atomic(self.home, index)
        back = memory_search._load_index(self.home)
        self.assertEqual(set(back), {"memory:a.md#0"})
        entry = back["memory:a.md#0"]
        self.assertEqual(entry["text_hash"], "abc")
        self.assertAlmostEqual(entry["mtime"], 12.5, places=6)
        for got, want in zip(entry["vector"], [0.25, -0.5, 0.75]):
            self.assertAlmostEqual(got, want, places=6)

    def test_the_store_is_sqlite_not_json(self):
        memory_search._write_index_atomic(
            self.home, {"memory:a.md#0": {"mtime": 1.0, "text_hash": "h",
                                          "vector": [1.0, 2.0]}})
        path = self.home / "memory" / "vectors.db"
        self.assertTrue(path.is_file())
        with sqlite3.connect(path) as conn:
            rows = conn.execute("SELECT key FROM vectors").fetchall()
        self.assertEqual(rows, [("memory:a.md#0",)])

    def test_a_key_that_is_gone_is_gone(self):
        memory_search._write_index_atomic(
            self.home, {"a#0": {"mtime": 1.0, "text_hash": "h",
                                "vector": [1.0]},
                        "b#0": {"mtime": 1.0, "text_hash": "h",
                                "vector": [2.0]}})
        memory_search._write_index_atomic(
            self.home, {"a#0": {"mtime": 1.0, "text_hash": "h",
                                "vector": [1.0]}})
        self.assertEqual(set(memory_search._load_index(self.home)), {"a#0"})

    def test_an_entry_with_no_vector_is_kept(self):
        """_refresh_index stores {mtime, text_hash} with no vector for a
        chunk whose embed failed and had no prior vector: the next pass
        must see it as known-but-unembedded, not as new."""
        memory_search._write_index_atomic(
            self.home, {"a#0": {"mtime": 1.0, "text_hash": "h"}})
        back = memory_search._load_index(self.home)
        self.assertIn("a#0", back)
        self.assertFalse(back["a#0"].get("vector"))

    def test_a_missing_store_is_none_not_a_crash(self):
        self.assertIsNone(memory_search._load_index(self.home))

    def test_a_corrupt_store_is_none_not_a_crash(self):
        (self.home / "memory" / "vectors.db").write_text("not a database")
        self.assertIsNone(memory_search._load_index(self.home))


class TestMigration(VectorStoreCase):
    def test_an_existing_json_index_is_migrated_once(self):
        (self.home / "memory" / "embeddings.json").write_text(json.dumps(
            {"memory:old.md#0": {"mtime": 9.0, "text_hash": "old",
                                 "vector": [0.5, 0.5]}}))
        back = memory_search._load_index(self.home)
        self.assertEqual(set(back), {"memory:old.md#0"})
        self.assertTrue((self.home / "memory" / "vectors.db").is_file(),
                        "the migration writes the new store")
        self.assertFalse((self.home / "memory" / "embeddings.json").exists(),
                         "and removes the old one, so it migrates once")

    def test_a_corrupt_json_index_does_not_block_the_migration(self):
        (self.home / "memory" / "embeddings.json").write_text("{ broken")
        self.assertIsNone(memory_search._load_index(self.home))


if __name__ == "__main__":
    unittest.main()


class TestForegroundBudget(VectorStoreCase):
    """A search must never pay for a whole backfill.

    `search()` calls `ensure_index(wait=False)`, which means "do not
    queue behind another pass", not "do not do the work": with the lock
    free, that search runs every embedding itself, in the foreground.
    Before 2026-09-21 a cousin had a few hundred chunks and that cost
    seconds; indexing the raw store multiplied the chunk count by about
    ten and a peer's first query sat over three minutes with the
    embedding service pinned. The daemon finishes the rest.
    """

    def _sources(self, n):
        for i in range(n):
            (self.home / "memory" / ("f%02d.md" % i)).write_text(
                "# file %d\n\nbody %d\n" % (i, i))

    def test_a_foreground_pass_stops_at_its_budget(self):
        from tests._fakes import fake_embedder
        self._sources(20)
        with fake_embedder() as url:
            report = memory_search.ensure_index(
                self.home, {"url": url, "model": "m", "timeout_s": 5},
                wait=False, budget=5)
        self.assertEqual(report["embedded"], 5)
        self.assertTrue(report["incomplete"],
                        "a bounded pass says it did not finish")

    def test_the_daemon_pass_is_not_bounded(self):
        from tests._fakes import fake_embedder
        self._sources(20)
        with fake_embedder() as url:
            report = memory_search.ensure_index(
                self.home, {"url": url, "model": "m", "timeout_s": 5})
        self.assertEqual(report["embedded"], 20)
        self.assertFalse(report["incomplete"])

    def test_a_bounded_pass_keeps_what_it_embedded(self):
        from tests._fakes import fake_embedder
        self._sources(20)
        cfg = {"url": None, "model": "m", "timeout_s": 5}
        with fake_embedder() as url:
            cfg["url"] = url
            memory_search.ensure_index(self.home, cfg, wait=False, budget=5)
            memory_search.ensure_index(self.home, cfg, wait=False, budget=5)
        self.assertEqual(len(memory_search._load_index(self.home)), 10,
                         "each pass adds its budget, none redoes the last")


class TestTheBudgetIsNotSilent(HermeticCase):
    """A bounded foreground pass must say so in the notice.

    `ensure_index` has always reported `incomplete`, but `search()`
    read only `busy` and `failed`, so the flag was produced and never
    displayed. Measured by a peer on a cold home 2026-09-22: the
    semantic leg ranked against 24 of 416 chunks, 5.8% of the corpus,
    and the call returned notice=None. Hits from 6% of a corpus that
    look like a complete result are how a librarian cites a confident
    wrong file. The tests below assert the flag where it is DISPLAYED;
    TestForegroundBudget asserts it where it is produced.
    """

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "live"
        self.home = self.root / "cousins" / "wren"
        (self.home / "memory").mkdir(parents=True)
        (self.root / "config").mkdir(parents=True)

    def _sources(self, n):
        for i in range(n):
            (self.home / "memory" / ("f%02d.md" % i)).write_text(
                "# file %d\n\nbody %d about zebracorn\n" % (i, i))

    def _search(self, url, budget):
        (self.root / "config" / "embedding.toml").write_text(
            'url = "%s"\nmodel = "m"\ntimeout_s = 5\n' % url)
        os.environ["FRAMEWORK_ROOT"] = str(self.root)
        with mock.patch.object(memory_search, "FOREGROUND_BUDGET", budget):
            return memory_search.search("zebracorn", home=self.home)

    def test_a_partial_semantic_leg_is_announced(self):
        from tests._fakes import fake_embedder
        self._sources(20)
        with fake_embedder() as url:
            hits, notice = self._search(url, 5)
        self.assertTrue(hits, "the keyword leg still serves")
        self.assertIsNotNone(
            notice, "a semantic leg over part of the corpus must not"
                    " come back looking complete")
        self.assertIn("5 of 20", notice,
                      "the notice quotes the coverage: how much of the"
                      " corpus was ranked by meaning")

    def test_a_finished_pass_stays_quiet(self):
        from tests._fakes import fake_embedder
        self._sources(20)
        with fake_embedder() as url:
            hits, notice = self._search(url, 50)
        self.assertIsNone(
            notice, "nothing was degraded, so nothing is announced")

    def test_the_report_carries_the_coverage(self):
        from tests._fakes import fake_embedder
        self._sources(20)
        with fake_embedder() as url:
            report = memory_search.ensure_index(
                self.home, {"url": url, "model": "m", "timeout_s": 5},
                wait=False, budget=5)
        self.assertEqual((report["ranked"], report["total"]), (5, 20))
